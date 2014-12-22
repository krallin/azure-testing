import os
import logging
import random
import base64
import uuid
import time

from azure import *
from azure.servicemanagement import *

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


subscription_id = "<sub-id>"
certificate_path = os.path.join(os.path.dirname(__file__), "mycert.pem")

sms = ServiceManagementService(subscription_id, certificate_path)

SERVICE_NAME = "scalr-test-5"
DEPLOYMENT_NAME = "vm-group"

VM_SIZE = "Small"

SERVICE_LOCATION = "West Europe"  # Location ends up controlled by service.

IMAGE_NAME = "b39f27a8b8c64d52b05eac6a62ebad85__Ubuntu-14_04_1-LTS-amd64-server-20141125-en-us-30GB"
NET_NAME = "Group Group thomas-tests"

# If using an Image
# Not a good idea, because there are no VM images for Ubuntu!!! (only a few SQL Server and HortonWorks boxes)
BLOB_CONTAINER = "https://testubuntug7v8mk8o.blob.core.windows.net/vhds/"

CLOUD_INIT_CUSTOM_DATA = """#cloud-config
ssh_import_id: [torozco]
"""
# In Theory: http://azure.microsoft.com/blog/2014/04/21/custom-data-and-cloud-init-on-windows-azure/

#for img in sms.list_vm_images():
#    print img.name, img.publisher_name
#    print img.__dict__
#    print
#exit(0)

#for net in sms.list_virtual_network_sites():
#    print net.name
#    for subnet in net.subnets:
#        print subnet.name, subnet.address_prefix
#
#ubuntu = sms.get_os_image(IMAGE_NAME)
#print ubuntu.__dict__

class OperationFailed(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message

    def __repr__(self):
        return "{0} ({1})".format(self.code, self.message)


def wait_for_operation(operation):
    while 1:
        status = sms.get_operation_status(operation.request_id)
        logger.debug("Operation status is '%s'", status.status)
        if status.status != "InProgress":
            break
        time.sleep(1)

    if status.error is not None:
        raise Operation(status.error.code, status.error.message)

    logger.debug("Operation '%s' completed with '%s'", operation.request_id, status.status)


def _get_os_disk_configuration_for_vm(vm_name):
    disk_name = "os-disk-{0}".format(vm_name)
    disk_url = "{0}/{1}.vhd".format(BLOB_CONTAINER, vm_name)  # /!\ This must be found under /vhds/ !! Can't use the root

    return OSVirtualHardDisk(
        source_image_name=IMAGE_NAME,
        disk_name=disk_name,
        media_link=disk_url
    )


def deploy_vm(vm_name, vm_ssh_port):
    # Does the service exist?
    try:
        service = sms.get_hosted_service_properties(SERVICE_NAME)
    except WindowsAzureMissingResourceError:
        logger.info("Service '%s' does not exist, creating", SERVICE_NAME)
        sms.create_hosted_service(service_name=SERVICE_NAME, label=SERVICE_NAME, location=SERVICE_LOCATION)
    else:
        real_location = service.hosted_service_properties.location
        if real_location != SERVICE_LOCATION:
            logger.warning("Service '%s' exists, but its Location is '%s', not: %s'.", SERVICE_NAME, real_location, SERVICE_LOCATION)


    # http://msdn.microsoft.com/en-us/library/azure/jj157194.aspx#bk_rolelist
    network_config = ConfigurationSet()
    network_config.input_endpoints.input_endpoints.append(ConfigurationSetInputEndpoint(
        name = "ssh",
        protocol = "tcp",
        local_port = 22,
        port = vm_ssh_port,
    ))
    network_config.subnet_names.append("Subnet-1")          # Deploy in subnet 1

    kwargs = {
        "service_name" : SERVICE_NAME,
        "deployment_name" : DEPLOYMENT_NAME,

        # Actual VM params
        "role_name" : vm_name,

        "os_virtual_hard_disk" : _get_os_disk_configuration_for_vm(vm_name),

        "system_config" : LinuxConfigurationSet(
            host_name = vm_name,
            user_name = "thomas",                           # Literally stuff you don't need
            user_password = base64.b64encode(open("/dev/urandom").read(32)),
            disable_ssh_password_authentication = True,
            custom_data = CLOUD_INIT_CUSTOM_DATA,           # Crossing fingers
        ),
        "network_config" : network_config,

        "role_size" : VM_SIZE,                                # Is this actually the instance type?!
    }

    try:
        deployment = sms.get_deployment_by_name(SERVICE_NAME, DEPLOYMENT_NAME)
    except WindowsAzureMissingResourceError:
        logger.info("Deployment '%s' does not exist in Service '%s'", DEPLOYMENT_NAME, SERVICE_NAME)
        method = sms.create_virtual_machine_deployment
        kwargs.update({
            "deployment_slot" : "Production",
            "label" : vm_name,
            "virtual_network_name" : NET_NAME,
        })
    else:
        method = sms.add_role

    logger.info("Creating VM: %s", vm_name)

    operation = method(**kwargs)

    try:
        wait_for_operation(operation)
    except OperationFailed as e:
        logger.error("An error occured: '%s'", e.message)
    else:
        logger.info("VM '%s' is ready, ssh to '%s.cloudapp.net -p %s'", vm_name, SERVICE_NAME, vm_ssh_port)


def teardown_hosted_service():
    try:
        service = sms.get_hosted_service_properties(SERVICE_NAME, embed_detail = True)
    except WindowsAzureMissingResourceError:
        logger.info("Service '%s' was already deleted", SERVICE_NAME)
        return

    for deployment in service.deployments:
        disks_to_delete = []

        # Deleting the Deployment tends to bug out and leave a few disks un-deletable for
        # while because they are still attached. We instead delete the VMs, then just poll
        # the disks until we can delete them.
        # NO - DOESNT WORK THIS WAY. CANT DELETE THE LAST VM UNLESS I DELETE THE DEPLOYMENT.
        for role in deployment.role_list:
            # Caution: delete_role_instances does not work for VMs!
#            logger.info("Deleting VM '%s'", role.role_name)
#            op = sms.delete_role(SERVICE_NAME, deployment.name, role.role_name)
#            wait_for_operation(op)
#
            disks_to_delete.append(role.os_virtual_hard_disk)

        logger.info("Deleting Deployment '%s'", deployment.name)
        op = sms.delete_deployment(SERVICE_NAME, deployment.name)
        wait_for_operation(op)

        while disks_to_delete:
            candidate = disks_to_delete.pop(0)

            if not hasattr(candidate, 'name'):
                # Disk has a name, but OSVirtualHardDisk has disk_name
                candidate.name = candidate.disk_name

            if hasattr(candidate, "attached_to"):
                # We've already seen this guy, it's a real disk. Pace ourselves.
                logger.debug("Disk '%s' was attached when we checked, waiting", candidate.name)
                time.sleep(10)

            # Check if any progress was made
            real_disk = sms.get_disk(candidate.name)
            if real_disk.attached_to is not None:
                logger.debug("Disk '%s' is still attached to '%s'", real_disk.name, real_disk.attached_to.role_name)
                disks_to_delete.append(real_disk)
                continue

            logger.info("Deleting Disk '%s'", real_disk.name)
            sms.delete_disk(real_disk.name, delete_vhd=True)


    logger.info("Deleting Service '%s'", deployment.name)
    sms.delete_hosted_service(SERVICE_NAME)



deploy_vm(str(uuid.uuid4()), random.randint(2**12, 2**16) - 1)
deploy_vm(str(uuid.uuid4()), random.randint(2**12, 2**16) - 1)
teardown_hosted_service()
