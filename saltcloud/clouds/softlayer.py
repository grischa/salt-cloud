'''
SoftLayer Cloud Module
===================

The SoftLayer cloud module is used to control access to the SoftLayer VPS system

Use of this module only requires the ``apikey`` parameter. Set up the cloud
configuration at:

``/etc/salt/cloud.providers`` or ``/etc/salt/cloud.providers.d/softlayer.conf``:

.. code-block:: yaml

    my-softlayer-config:
      # SoftLayer account api key
      user: MYLOGIN
      apikey: JVkbSJDGHSDKUKSDJfhsdklfjgsjdkflhjlsdfffhgdgjkenrtuinv
      provider: softlayer

'''

# Import python libs
import pprint
import logging
import time

# Import libcloud
from libcloud.compute.base import NodeAuthPassword

# Import salt libs
import salt.utils.xmlutil

# Import salt cloud libs
import saltcloud.config as config
from saltcloud.libcloudfuncs import *   # pylint: disable-msg=W0614,W0401
from saltcloud.utils import namespaced_function

# Attempt to import softlayer lib
try:
    import SoftLayer.API
    HAS_SLLIBS = True
except Exception as exc:
    HAS_SLLIBS = False

# Get logging started
log = logging.getLogger(__name__)


# Redirect SoftLayer functions to this module namespace
script = namespaced_function(script, globals())


# Only load in this module if the SoftLayer configurations are in place
def __virtual__():
    '''
    Set up the libcloud functions and check for SoftLayer configurations.
    '''
    if get_configured_provider() is False:
        log.debug(
            'There is no SoftLayer cloud provider configuration available. Not '
            'loading module.'
        )
        return False

    log.debug('Loading SoftLayer cloud module')
    return True


def get_configured_provider():
    '''
    Return the first configured instance.
    '''
    return config.is_provider_configured(
        __opts__,
        __active_provider_name__ or 'softlayer',
        ('apikey',)
    )


def get_conn(service='SoftLayer_Virtual_Guest'):
    '''
    Return a conn object for the passed VM data
    '''
    client = SoftLayer.API.Client(
        service,
        None,
        config.get_config_value(
            'user', get_configured_provider(), __opts__, search_global=False
        ),
        config.get_config_value(
            'apikey', get_configured_provider(), __opts__, search_global=False
        ),
    )
    return client


def avail_locations():
    '''
    List all available locations
    '''
    ret = {}
    conn = get_conn()
    response = conn.getCreateObjectOptions()
    #return response
    for datacenter in response['datacenters']:
        #return datacenter
        ret[datacenter['template']['datacenter']['name']] = {
            'name': datacenter['template']['datacenter']['name'],
        }
    return ret


def avail_sizes():
    '''
    Return a dict of all available VM sizes on the cloud provider with
    relevant data. This data is provided in three dicts.

    '''
    ret = {
        'block devices': {},
        'memory': {},
        'processors': {},
    }
    conn = get_conn()
    response = conn.getCreateObjectOptions()
    for device in response['blockDevices']:
        #return device['template']['blockDevices']
        ret['block devices'][device['itemPrice']['item']['description']] = {
            'name': device['itemPrice']['item']['description'],
            'capacity':
                device['template']['blockDevices'][0]['diskImage']['capacity'],
        }
    for memory in response['memory']:
        ret['memory'][memory['itemPrice']['item']['description']] = {
            'name': memory['itemPrice']['item']['description'],
            'maxMemory': memory['template']['maxMemory'],
        }
    for processors in response['processors']:
        ret['processors'][processors['itemPrice']['item']['description']] = {
            'name': processors['itemPrice']['item']['description'],
            'start cpus': processors['template']['startCpus'],
        }
    return ret


def avail_images():
    '''
    Return a dict of all available VM images on the cloud provider.
    '''
    ret = {}
    conn = get_conn()
    response = conn.getCreateObjectOptions()
    for image in response['operatingSystems']:
        ret[image['itemPrice']['item']['description']] = {
            'name': image['itemPrice']['item']['description'],
            'template': image['template']['operatingSystemReferenceCode'],
        }
    return ret


def get_location(vm_=None):
    '''
    Return the location to use, in this order:
        - CLI parameter
        - VM parameter
        - Cloud profile setting
    '''
    return __opts__.get(
        'location',
        config.get_config_value(
            'location',
            vm_ or get_configured_provider(),
            __opts__,
            #default=DEFAULT_LOCATION,
            search_global=False
        )
    )


def create(vm_):
    '''
    Create a single VM from a data dict
    '''
    log.info('Creating Cloud VM {0}'.format(vm_['name']))
    conn = get_conn()
    kwargs = {
        'hostname': vm_['name'],
        'domain': vm_['domain'],
        'operatingSystemReferenceCode': vm_['image'],
        'startCpus': vm_['cpu_number'],
        'maxMemory': vm_['ram'],
        'localDiskFlag': vm_['local_disk'],
        'blockDevices': [{
            'device': '0',
            'diskImage': {'capacity': vm_['disk_size']},
        }],
        'hourlyBillingFlag': vm_['hourly_billing'],
    }

    location = get_location(vm_)
    if location:
        kwargs['datacenter'] = {'name': location}

    try:
        response = conn.createObject(kwargs)
    except Exception as exc:
        log.error(
            'Error creating {0} on SoftLayer\n\n'
            'The following exception was thrown by libcloud when trying to '
            'run the initial deployment: \n{1}'.format(
                vm_['name'], exc.message
            ),
            # Show the traceback if the debug logging level is enabled
            exc_info=log.isEnabledFor(logging.DEBUG)
        )
        return False

    def wait_for_ip():
        '''
        Wait for the IP address to become available
        '''
        nodes = list_nodes_full()
        if 'primaryIpAddress' in nodes[vm_['name']]:
            return nodes[vm_['name']]['primaryIpAddress']
        time.sleep(1)
        return False

    ip_address = saltcloud.utils.wait_for_fun(wait_for_ip)

    if not saltcloud.utils.wait_for_ssh(ip_address):
        raise SaltCloudSystemExit(
            'Failed to authenticate against remote ssh'
        )

    pass_conn = get_conn(service='SoftLayer_Account')
    mask = {
        'virtualGuests': {
            'powerState': '',
            'operatingSystem': {
                'passwords': ''
            },
        },
    }

    def get_passwd():
        '''
        Wait for the password to become available
        '''
        node_info = pass_conn.getVirtualGuests(id=response['id'], mask=mask)
        for node in node_info:
            if node['id'] == response['id']:
                if 'passwords' in node['operatingSystem'] and len(node['operatingSystem']['passwords']) > 0:
                    return node['operatingSystem']['passwords'][0]['password']
        time.sleep(5)
        return False

    passwd = saltcloud.utils.wait_for_fun(get_passwd)
    response['password'] = passwd
    response['public_ip'] = ip_address

    ret = {}
    if config.get_config_value('deploy', vm_, __opts__) is True:
        deploy_script = script(vm_)
        deploy_kwargs = {
            'host': ip_address,
            'username': 'root',
            'password': passwd,
            'script': deploy_script.script,
            'name': vm_['name'],
            'deploy_command': '/tmp/deploy.sh',
            'start_action': __opts__['start_action'],
            'parallel': __opts__['parallel'],
            'sock_dir': __opts__['sock_dir'],
            'conf_file': __opts__['conf_file'],
            'minion_pem': vm_['priv_key'],
            'minion_pub': vm_['pub_key'],
            'keep_tmp': __opts__['keep_tmp'],
            'preseed_minion_keys': vm_.get('preseed_minion_keys', None),
            'display_ssh_output': config.get_config_value(
                'display_ssh_output', vm_, __opts__, default=True
            ),
            'script_args': config.get_config_value(
                'script_args', vm_, __opts__
            ),
            'script_env': config.get_config_value('script_env', vm_, __opts__),
            'minion_conf': saltcloud.utils.minion_config(__opts__, vm_)
        }

        # Deploy salt-master files, if necessary
        if config.get_config_value('make_master', vm_, __opts__) is True:
            deploy_kwargs['make_master'] = True
            deploy_kwargs['master_pub'] = vm_['master_pub']
            deploy_kwargs['master_pem'] = vm_['master_pem']
            master_conf = saltcloud.utils.master_config(__opts__, vm_)
            deploy_kwargs['master_conf'] = master_conf

            if master_conf.get('syndic_master', None):
                deploy_kwargs['make_syndic'] = True

        deploy_kwargs['make_minion'] = config.get_config_value(
            'make_minion', vm_, __opts__, default=True
        )

        # Store what was used to the deploy the VM
        ret['deploy_kwargs'] = deploy_kwargs

        deployed = saltcloud.utils.deploy_script(**deploy_kwargs)
        if deployed:
            log.info('Salt installed on {0}'.format(vm_['name']))
        else:
            log.error(
                'Failed to start Salt on Cloud VM {0}'.format(
                    vm_['name']
                )
            )

    log.info('Created Cloud VM {0[name]!r}'.format(vm_))
    log.debug(
        '{0[name]!r} VM creation details:\n{1}'.format(
            vm_, pprint.pformat(response)
        )
    )

    ret.update(response)
    return ret


def list_nodes_full(mask='id'):
    '''
    Return a list of the VMs that are on the provider
    '''
    ret = {}
    conn = get_conn()
    response = conn['Account'].getVirtualGuests(mask=mask)
    for node_id in response:
        node_info = conn.getObject(id=node_id['id'])
        ret[node_info['hostname']] = node_info
    return ret


def list_nodes():
    '''
    Return a list of the VMs that are on the provider
    '''
    ret = {}
    nodes = list_nodes_full()
    if 'error' in nodes:
        raise SaltCloudSystemExit(
            'An error occurred while listing nodes: {0}'.format(
                nodes['error']['Errors']['Error']['Message']
            )
        )
    for node in nodes:
        ret[node] = {
            'id': nodes[node]['hostname'],
            'ram': nodes[node]['maxMemory'],
            'cpus': nodes[node]['maxCpu'],
            'private_ips': nodes[node]['primaryBackendIpAddress'],
            'public_ips': nodes[node]['primaryIpAddress'],
        }
    return ret


def list_nodes_select():
    '''
    Return a list of the VMs that are on the provider, with select fields
    '''
    ret = {}

    nodes = list_nodes_full()
    if 'error' in nodes:
        raise SaltCloudSystemExit(
            'An error occurred while listing nodes: {0}'.format(
                nodes['error']['Errors']['Error']['Message']
            )
        )

    for node in nodes:
        pairs = {}
        data = nodes[node]
        for key in data:
            if str(key) in __opts__['query.selection']:
                value = data[key]
                pairs[key] = value
        ret[node] = pairs

    return ret

def show_instance(name, call=None):
    '''
    Show the details from SoftLayer concerning a guest
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The show_instance action must be called with -a or --action.'
        )

    nodes = list_nodes_full()
    return nodes[name]


def destroy(name, call=None):
    '''
    Destroy a node.

    CLI Example::

        salt-cloud --destroy mymachine
    '''
    ret = {}
    node = show_instance(name, call='action')
    conn = get_conn()
    response = conn.deleteObject(id=node['id'])
    return response

    node = show_instance(name, call='action')
    if node['state'] == 'STARTED':
        stop(name, call='action')
        if not wait_until(name, 'STOPPED'):
            return {
                'Error': 'Unable to destroy {0}, command timed out'.format(
                    name
                )
            }

    data = query(action='ve', command=name, method='DELETE')

    if 'error' in data:
        return data['error']

    return {'Destroyed': '{0} was destroyed.'.format(name)}


