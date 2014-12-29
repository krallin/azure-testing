#coding:utf-8
import argparse
import base64
import socket
import logging
import random
import uuid
import time

from attrdict import load as attrdict_load
from azure import WindowsAzureMissingResourceError
from azure.servicemanagement import ServiceManagementService, ConfigurationSet, ConfigurationSetInputEndpoint, PublicIP, \
    OSVirtualHardDisk, DataVirtualHardDisks, DataVirtualHardDisk, LinuxConfigurationSet
from azure.storage import BlobService


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


# noinspection PyUnusedLocal
def ssh_up(name, host, port, timeout=5):
    try:
        s = socket.create_connection((host, port), timeout)
        s.settimeout(timeout)
        data = s.recv(64).strip()
    except (socket.error, socket.timeout) as e:
        logger.debug("SSH connection failed with: %s", e)
        return False

    if not data.startswith('SSH'):
        logger.warning("SSH did not respond with 'SSH' header: '%s'", data)
        return False

    logger.debug("SSH responded with 'SSH' header: '%s'", data)
    return True


class OperationFailed(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message

    def __str__(self):
        return "{0} ({1})".format(self.code, self.message)


def wait_for_operation(sms, operation):

    while 1:
        status = sms.get_operation_status(operation.request_id)
        logger.debug("Operation status is '%s'", status.status)
        if status.status != "InProgress":
            break
        time.sleep(1)

    # noinspection PyUnboundLocalVariable
    if status.error is not None:
        logger.error("Request failed: %s (%s)", status.error.code, status.error.message)
        raise OperationFailed(status.error.code, status.error.message)

    logger.info("Operation '%s' completed with '%s'", operation.request_id, status.status)


def random_vm_name():
    return str(uuid.uuid4())

def random_port():
    return random.randint(2**12, 2**16) - 1


def create_hosted_service(sms, service_name, service_location):
    # Does the service exist?
    try:
        service = sms.get_hosted_service_properties(service_name)
    except WindowsAzureMissingResourceError:
        logger.info("Service '%s' does not exist, creating", service_name)
        sms.create_hosted_service(service_name=service_name, label=service_name, location=service_location)
    else:
        real_location = service.hosted_service_properties.location
        if real_location != service_location:
            logger.warning("Service '%s' exists, but its Location is '%s', not: %s'.", service_name, real_location, service_location)


def deploy_vm(sms, bs, service_name, deployment_name, network_name, vhds_container, vm_config):

    vm_name = random_vm_name()
    format_kwargs = {"vm_name": vm_name}

    logger.info("Deploying '%s' in '%s/%s'", vm_name, service_name, deployment_name)

    ##################
    # Disk Container #
    ##################
    # We must first ensure that the container where we will place the VHDs for the VM exists. The container is a
    # directory within the Storage Account (somewhat akin to a bucket in AWS).

    bs.create_container(vhds_container, fail_on_exist=False)


    #########################
    # Network Configuration #
    #########################
    # NOTE: This only depends on vm_name and vm_config, so it could be extracted into a function

    network_configuration = ConfigurationSet()

    for nat_port in vm_config.net.nat_ports:
        # At a minimum, a NAT port requires the traffic we expect to serve.
        nat_port_kwargs = {
            "name": nat_port.name,
            "protocol": nat_port.protocol,
            "local_port": nat_port.port
        }
        if nat_port.lb:
            # If a port is load-balanced, multiple VMs can use the same one, but we must indicate to the
            # API that this is what we wanted to do.
            nat_port_kwargs.update({
                "port": nat_port.port,
                "load_balanced_endpoint_set_name": "lb-{0}".format(nat_port.name)
            })
        else:
            # If the port isn't load balanced, then each VM gets its own random port.
            nat_port_kwargs.update({
                "port": random_port()
            })
        network_configuration.input_endpoints.input_endpoints.append(ConfigurationSetInputEndpoint(**nat_port_kwargs))

    for subnet_name in vm_config.net.subnet_names:
        # All these Subnets must belong to the Virtual Network used for the VM Deployment.
        # Note that it appears that providing multiple subnets is not useful, and results in the
        # first subnet only being used (at least according to the console).
        network_configuration.subnet_names.append(subnet_name)

    for public_ip_name_tpl in vm_config.net.public_ip_name_tpls:
        # Note that for some reason, not giving the IPs a name results in some form of conflict (which does *not* throw an error),
        # where only one instance will get a Public IP. Giving the IPs a name seems to resolve the issue (note that it doesn't seem to
        # matter whether that name is unique or not, but if we're giving the IP a name, we might as well make it unique)
        network_configuration.public_ips.public_ips.append(PublicIP(name=public_ip_name_tpl.format(**format_kwargs)))


    ###########################
    # Root Disk Configuration #
    ###########################
    # NOTE: Same as above

    # Disks are composed of two things:
    #   + A "disk" entity that can be attached to VMs, and is little more than a pointer to the underlying blob.
    #   + A "blob" that is stored in Azure's blob storage, and is the actual container for the disk bytes.

    # In this case, our disk is a OS disk, so it also has a source Image. Source images are OS Images, and they
    # can be listed using: `sms.list_os_images()`.

    disk_name = vm_config.root_disk.name_tpl.format(**format_kwargs)
    disk_url = bs.make_blob_url(vhds_container, "{disk_name}.vhd".format(disk_name=disk_name))

    root_disk_configuration = OSVirtualHardDisk(
        source_image_name = vm_config.root_disk.source_image,
        disk_name = disk_name,
        media_link = disk_url
    )


    #############################
    # Extra Disks Configuration #
    #############################
    # NOTE: Same as above

    # Configure some extra disks for this host. Note that there are some limitations on the number of disks that may be
    # attached to a VM: http://msdn.microsoft.com/en-us/library/dn197896.aspx

    extra_disks_configuration = DataVirtualHardDisks()
    for i, disk_config in enumerate(vm_config.data_disks):
        # It is beyond me why the SDK doesn't allow us to configure this disk through the constructor.
        disk = DataVirtualHardDisk()
        disk.lun = i
        disk.media_link = disk_config.url_tpl.format(**format_kwargs)
        disk.logical_disk_size_in_gb = disk_config.size_gb
        extra_disks_configuration.data_virtual_hard_disks.append(disk)


    ####################
    # OS Configuration #
    ####################
    # NOTE: Same as above

    # This configuration is a mixed bag of things we don't need that are required, and things we don't need that
    # aren't. Specifically, the agent (waagent) that Azures has on our VM requires that we create a new user,
    # and give it a password. We don't really want to use that, but we don't really have a choice, either.

    # Note that the agent seems optional, and we can pass `provision_guest_agent = False` to the
    # `sms.create_virtual_machine_deployment` / `sms.add_role` API Call to indicate that.

    # The host name is mandatory too, and will be (as far as I can tell) processed by waagent too. However,
    # the `custom_data` will (fortunately) be passed as user-data that will be processed by cloud-init:
    # http://azure.microsoft.com/blog/2014/04/21/custom-data-and-cloud-init-on-windows-azure/

    system_configuration = LinuxConfigurationSet(
        # Plenty of stuff we don't need!
        user_name = "thomas",
        user_password = base64.b64encode(open("/dev/urandom").read(32)),
        disable_ssh_password_authentication = True,

        # Finally, something we need!
        host_name = vm_config.system.host_name_tpl.format(**format_kwargs),
        custom_data = vm_config.system.user_data_tpl.format(**format_kwargs),
    )

    ############
    # API Call #
    ############

    # Finally, prepare the kwargs for the API Call. The `service_name` and `deployment_name` kwargs are
    # only needed ot identify what Deployment we want to add the VM to. The rest is configuration for
    # the VM itself.

    kwargs = {
        # Identify what this VM belongs to
        "service_name" : service_name,
        "deployment_name" : deployment_name,

        # Actual VM creation params
        "role_name" : vm_name,
        "os_virtual_hard_disk" : root_disk_configuration,
        "data_virtual_hard_disks": extra_disks_configuration,
        "system_config" : system_configuration,
        "network_config" : network_configuration,

        "role_size" : vm_config.size,
    }

    # One trick in the Azure API is that a VM Deployment can only be created when you create its first VM
    # (and symetrically, you can't delete the last VM of a Deployment, and must delete the Deployment
    # altogether).
    # What we do here is check whether the Deployment already exists. If it doesn't exist, then we use the
    # `create_virtual_machine_deployment` API Call to create it and create our VM. If it does exist, then
    # we just add a new VM to the Deployment.

    try:
        sms.get_deployment_by_name(service_name, deployment_name)
    except WindowsAzureMissingResourceError:
        logger.info("Deployment '%s' does not exist in Service '%s'", deployment_name, service_name)
        method = sms.create_virtual_machine_deployment
        # The kwargs we add here are required for the creation of a Deployment. Specfically, we need to pass
        # a label (this is an additional name: we don't really care and just use the name), and a Virtual Network
        # the Deployment should be added in.

        # NOTE: I haven't confirmed it, but I suppose this Virtual network must exist within the same Location
        # as the Hosted Service we are adding the Deployment to.
        kwargs.update({
            # Suppposedly, this *has* to be "Production", though in my experience you can pass something else.
            # I have no idea whether this is simply ignored, or whether it actually does something.
            "deployment_slot" : "Production",
            # These are useful kwargs:
            "label" : vm_name,
            "virtual_network_name" : network_name,
        })
    else:
        # We're just adding a new VM, no additional kwargs are required.
        method = sms.add_role

    # Finally, we make the call. It's asynchronous, so we wait on it.
    operation = method(**kwargs)
    wait_for_operation(sms, operation)
    logger.info("VM '%s' is ready", vm_name)


def test_ssh(sms, service_name, deployment_name):
    logger.info("Retrieving list of VMs and ports")
    deployment = sms.get_deployment_by_name(service_name, deployment_name)
    ssh_targets = []

    # Let's stop for a minute to talk about deployment.role_instance_list and deployment.role_list.

    # `deployment.role_list` is pretty much a list of what you requested when you provisioned the instance. It includes
    # the VHDs associated with it, the configuration you passed, etc.

    # `deployment.role_instance_list` corresponds to what actually exists in Azure. Among other things, this is where
    # we can find the IP of the instance's public endpoint (the Hosted Service).

    # It's also possible that one of those calls could return data for non-VM deployments (possibly role_list, which has
    # a role_type field in the objects it returns).
    # Fortunately, every API Call returns both items.

    for vm in deployment.role_instance_list:
        for endpoint in vm.instance_endpoints:
            # Note: the endpoint ports are strings. Not that this makes sense, but we have to convert
            # those to ints.
            if int(endpoint.local_port) == 22:
                ssh_targets.append((vm.instance_name, endpoint.vip, int(endpoint.public_port)))
        if not vm.public_ips:
            logger.warning("VM %s has no public IPs", vm.instance_name)
        else:
            for public_ip in vm.public_ips:
                ssh_targets.append((vm.instance_name, public_ip.address, 22))

    logger.info("SSH Targets: %s", ssh_targets)
    while ssh_targets:
        ssh_target = ssh_targets.pop(0)
        logger.debug("Trying to hit SSH on %s at %s:%s", *ssh_target)
        if ssh_up(*ssh_target):
            logger.info("SSH is up on %s at %s:%s", *ssh_target)
        else:
            logger.info("SSH is not up on %s at %s:%s", *ssh_target)
            ssh_targets.append(ssh_target)
            time.sleep(1)


def teardown(sms, service_name):
    # NOTE: This does NOT delete OS Images!
    try:
        service = sms.get_hosted_service_properties(service_name, embed_detail = True)
    except WindowsAzureMissingResourceError:
        logger.info("Service '%s' was already deleted", service_name)
        return

    for deployment in service.deployments:
        disks_to_delete = []

        # Deleting a Deployment (or deleting VMs, for that matter) will not delete the Disks associated with them,
        # so we start by getting a list of the Disks we'll need to cleanup.
        for role in deployment.role_list:
            disks_to_delete.append(role.os_virtual_hard_disk)
            disks_to_delete.extend(role.data_virtual_hard_disks)
            # Note that we shouldn't try to also delete the VMs here. This would fail for the last VM because you need
            # to delete the entire Deployment when deleting the last VM.

        logger.info("Deleting Deployment '%s'", deployment.name)
        op = sms.delete_deployment(service_name, deployment.name)
        wait_for_operation(sms, op)

        while disks_to_delete:
            candidate = disks_to_delete.pop(0)

            if not hasattr(candidate, 'name'):
                # Disk has a name, but OSVirtualHardDisk has disk_name
                candidate.name = candidate.disk_name

            if hasattr(candidate, "attached_to"):
                # We've already seen this guy, it's a real disk. Pace ourselves.
                logger.debug("Disk '%s' was attached when we checked, waiting", candidate.name)
                time.sleep(10)

            # Although we waited for the delete operation to complete on the Deployment, Azure actually doesn't guarantee
            # (though they don't document that anywhere) that our disks will be detached when that operation has completed.
            # In fact, the disks will always remain attached for a little while after the VM has been deleted. We therefore
            # poll the disks until they have finally been detached, before attempting to delete them (attempting to delete
            # an attached disk would of course fail).
            real_disk = sms.get_disk(candidate.name)
            if real_disk.attached_to is not None:
                logger.debug("Disk '%s' is still attached to '%s'", real_disk.name, real_disk.attached_to.role_name)
                disks_to_delete.append(real_disk)
                continue

            # Disk is no longer attached; we can finally delete. We make sure to also delete the underlying VHD (i.e. the blob
            # in Azure storage)
            logger.info("Deleting Disk '%s'", real_disk.name)
            sms.delete_disk(real_disk.name, delete_vhd=True)


    logger.info("Deleting Service '%s'", service_name)
    sms.delete_hosted_service(service_name)


def snapshot(sms, bs, service_name, deployment_name, images_container, snapshot_config):
    # Ensure that the Images container exists
    bs.create_container(images_container, fail_on_exist=False)


    # This is where things get really weird. We have two lists: one that contains root volumes, and
    # one that contains instance states!
    deployment = sms.get_deployment_by_name(service_name, deployment_name)
    role_names_to_vhds = dict(((role.role_name, role.os_virtual_hard_disk) for role in deployment.role_list))

    for role in deployment.role_instance_list:
        logger.info("Preparing to snapshot '%s'. Make you sure you ran `waagent --deallocate`!", role.role_name)

        if role.instance_status not in ("Stopped", "StoppedDeallocated"):
            logger.warning("Role '%s' is in '%s' state. Snapshotting is risky!", role.role_name, role.instance_status)

        # TODO - Lease problem here? Seems like there isn't.
        root_vhd = role_names_to_vhds[role.role_name]

        dst_blob_name = "image-from-{0}.vhd".format(role.role_name)
        dst_blob_url = bs.make_blob_url(images_container, dst_blob_name)
        bs.copy_blob(images_container, dst_blob_name, root_vhd.media_link)


        format_kwargs = {"role": role}
        os_label = snapshot_config.label_tpl.format(**format_kwargs)
        os_name = snapshot_config.name_tpl.format(**format_kwargs)
        os_type = snapshot_config.os

        op = sms.add_os_image(os_label, dst_blob_url, os_name, os_type)
        wait_for_operation(sms, op)

        logger.info("Created OS: '%s'", os_name)


def start(sms, service_name, deployment_name):
    logger.info("Starting all Roles in '%s/%s'", service_name, deployment_name)
    deployment = sms.get_deployment_by_name(service_name, deployment_name)
    op = sms.start_roles(service_name, deployment_name, [role.role_name for role in deployment.role_list])
    wait_for_operation(sms, op)


def stop(sms, service_name, deployment_name):
    logger.info("Stopping all Roles in '%s/%s'", service_name, deployment_name)
    deployment = sms.get_deployment_by_name(service_name, deployment_name)

    # Here, Azure gives us the option to shutdown and stop paying for the instance (StoppedDeallocated), or just to
    # shut down and continue paying. It's unclear what are the benefits of continuing to pay (maybe the VM boots
    # up faster next time, and maybe the IP is retained). For demonstration purposes, we de-allocate the VM.
    op = sms.shutdown_roles(service_name, deployment_name, [role.role_name for role in deployment.role_list],
                            post_shutdown_action="StoppedDeallocated")
    wait_for_operation(sms, op)



def main():
    #################
    # Configuration #
    #################

    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", required=True)
    parser.add_argument('--provision', dest="vm_config", help="Provision the VM as specifified in this file.")
    parser.add_argument('--start', action='store_true', help="Start all the VMs")
    parser.add_argument('--test-ssh', action='store_true', help="Test SSH on the VMs")
    parser.add_argument('--stop', action='store_true', help="Stop all the VMs")
    parser.add_argument('--snapshot', dest="snapshot_config", help="Snapshot all the VMs")
    parser.add_argument('--teardown', action='store_true', help="Teardown the cluster (note: OS Images aren't deleted)")
    ns = parser.parse_args()

    # Those keys MUST be in the configuration file
    config = attrdict_load(ns.config)

    ###################
    # Actual Workflow #
    ###################

    # Service Management is for VMs. Storage Service is for blobs (VHDs that back the VMs). The credentials
    # use by each service are different.
    sms = ServiceManagementService(config.service_management.subscription_id, config.service_management.certificate_path)
    bs = BlobService(config.storage.account, config.storage.access_key)

    if ns.vm_config:
        create_hosted_service(sms, config.service_name, config.service_location)

        vm_config = attrdict_load(ns.vm_config)
        for _ in range(config.n_vms):
            deploy_vm(sms, bs, config.service_name, config.deployment_name, config.network_name, config.containers.vhds, vm_config)

    if ns.start:
        start(sms, config.service_name, config.deployment_name)

    if ns.test_ssh:
        test_ssh(sms, config.service_name, config.deployment_name)

    if ns.stop:
        stop(sms, config.service_name, config.deployment_name)

    if ns.snapshot_config:
        snapshot_config = attrdict_load(ns.snapshot_config)
        snapshot(sms, bs, config.service_name, config.deployment_name, config.containers.images, snapshot_config)

    if ns.teardown:
        teardown(sms, config.service_name)


if __name__ == "__main__":
    main()
