import ipaddress
import os
import re
from tempfile import mkstemp

from box import Box
from broker import Broker
from fauxfactory import gen_string
from packaging.version import Version
import pytest

from robottelo import constants
from robottelo.config import settings
from robottelo.hosts import ContentHost


@pytest.fixture(scope='module')
def module_provisioning_capsule(module_target_sat, module_location):
    """Assigns the `module_location` to Satellite's internal capsule and returns it"""
    capsule = module_target_sat.nailgun_smart_proxy
    capsule.location = [module_location]
    return capsule.update(['location'])


@pytest.fixture(scope='module')
def module_provisioning_rhel_content(
    request,
    module_provisioning_sat,
    module_sca_manifest_org,
    module_lce_library,
):
    """
    This fixture sets up kickstart repositories for a specific RHEL version
    that is specified in `request.param`.
    """
    sat = module_provisioning_sat.sat
    rhel_ver = request.param['rhel_version']
    repo_names = []
    if int(rhel_ver) <= 7:
        repo_names.append(f'rhel{rhel_ver}')
    else:
        repo_names.append(f'rhel{rhel_ver}_bos')
        repo_names.append(f'rhel{rhel_ver}_aps')
    rh_repos = []
    tasks = []
    rh_repo_id = ""
    content_view = sat.api.ContentView(organization=module_sca_manifest_org).create()

    # Custom Content for Client repo
    custom_product = sat.api.Product(
        organization=module_sca_manifest_org, name=f'rhel{rhel_ver}_{gen_string("alpha")}'
    ).create()
    client_repo = sat.api.Repository(
        organization=module_sca_manifest_org,
        product=custom_product,
        content_type='yum',
        url=settings.repos.SATCLIENT_REPO[f'rhel{rhel_ver}'],
    ).create()
    task = client_repo.sync(synchronous=False)
    tasks.append(task)
    content_view.repository = [client_repo]

    for name in repo_names:
        rh_kickstart_repo_id = sat.api_factory.enable_rhrepo_and_fetchid(
            basearch=constants.DEFAULT_ARCHITECTURE,
            org_id=module_sca_manifest_org.id,
            product=constants.REPOS['kickstart'][name]['product'],
            repo=constants.REPOS['kickstart'][name]['name'],
            reposet=constants.REPOS['kickstart'][name]['reposet'],
            releasever=constants.REPOS['kickstart'][name]['version'],
        )
        # do not sync content repos for discovery based provisioning.
        if module_provisioning_sat.provisioning_type != 'discovery':
            rh_repo_id = sat.api_factory.enable_rhrepo_and_fetchid(
                basearch=constants.DEFAULT_ARCHITECTURE,
                org_id=module_sca_manifest_org.id,
                product=constants.REPOS[name]['product'],
                repo=constants.REPOS[name]['name'],
                reposet=constants.REPOS[name]['reposet'],
                releasever=constants.REPOS[name]['releasever'],
            )

        # Sync step because repo is not synced by default
        for repo_id in [rh_kickstart_repo_id, rh_repo_id]:
            if repo_id:
                rh_repo = sat.api.Repository(id=repo_id).read()
                task = rh_repo.sync(synchronous=False)
                tasks.append(task)
                rh_repos.append(rh_repo)
                content_view.repository.append(rh_repo)
                content_view.update(['repository'])
    for task in tasks:
        sat.wait_for_tasks(
            search_query=(f'id = {task["id"]}'),
            poll_timeout=2500,
        )
        task_status = sat.api.ForemanTask(id=task['id']).poll()
        assert task_status['result'] == 'success'
    rhel_xy = Version(
        constants.REPOS['kickstart'][f'rhel{rhel_ver}']['version']
        if rhel_ver == 7
        else constants.REPOS['kickstart'][f'rhel{rhel_ver}_bos']['version']
    )
    o_systems = sat.api.OperatingSystem().search(
        query={'search': f'family=Redhat and major={rhel_xy.major} and minor={rhel_xy.minor}'}
    )
    assert o_systems, f'Operating system RHEL {rhel_xy} was not found'
    os = o_systems[0].read()
    # return only the first kickstart repo - RHEL X KS or RHEL X BaseOS KS
    ksrepo = rh_repos[0]
    publish = content_view.publish()
    task_status = sat.wait_for_tasks(
        search_query=(f'Actions::Katello::ContentView::Publish and id = {publish["id"]}'),
        search_rate=15,
        max_tries=10,
    )
    assert task_status[0].result == 'success'
    content_view = sat.api.ContentView(
        organization=module_sca_manifest_org, name=content_view.name
    ).search()[0]
    ak = sat.api.ActivationKey(
        organization=module_sca_manifest_org,
        content_view=content_view,
        environment=module_lce_library,
    ).create()

    return Box(os=os, ak=ak, ksrepo=ksrepo, cv=content_view)


@pytest.fixture(scope='module')
def module_provisioning_sat(
    request,
    module_target_sat,
    module_sca_manifest_org,
    module_location,
    module_provisioning_capsule,
):
    """
    This fixture sets up the Satellite for PXE provisioning.
    It calls a workflow using broker to set up the network and to run satellite-installer.
    It uses the artifacts from the workflow to create all the necessary Satellite entities
    that are later used by the tests.
    """
    provisioning_type = getattr(request, 'param', '')
    sat = module_target_sat
    provisioning_domain_name = f"{gen_string('alpha').lower()}.foo"

    broker_data_out = Broker().execute(
        workflow=settings.provisioning.provisioning_sat_workflow,
        artifacts='last',
        target_vlan_id=settings.provisioning.vlan_id,
        target_host=sat.name,
        provisioning_dns_zone=provisioning_domain_name,
        sat_version=sat.version,
    )

    broker_data_out = Box(**broker_data_out['data_out'])
    provisioning_interface = ipaddress.ip_interface(broker_data_out.provisioning_addr_ipv4)
    provisioning_network = provisioning_interface.network
    # TODO: investigate DNS setup issue on Satellite,
    # we might need to set up Sat's DNS server as the primary one on the Sat host
    provisioning_upstream_dns_primary = (
        broker_data_out.provisioning_upstream_dns.pop()
    )  # There should always be at least one upstream DNS
    provisioning_upstream_dns_secondary = (
        broker_data_out.provisioning_upstream_dns.pop()
        if len(broker_data_out.provisioning_upstream_dns)
        else None
    )

    domain = sat.api.Domain(
        location=[module_location],
        organization=[module_sca_manifest_org],
        dns=module_provisioning_capsule.id,
        name=provisioning_domain_name,
    ).create()

    subnet = sat.api.Subnet(
        location=[module_location],
        organization=[module_sca_manifest_org],
        network=str(provisioning_network.network_address),
        mask=str(provisioning_network.netmask),
        gateway=broker_data_out.provisioning_gw_ipv4,
        from_=broker_data_out.provisioning_host_range_start,
        to=broker_data_out.provisioning_host_range_end,
        dns_primary=provisioning_upstream_dns_primary,
        dns_secondary=provisioning_upstream_dns_secondary,
        boot_mode='DHCP',
        ipam='DHCP',
        dhcp=module_provisioning_capsule.id,
        tftp=module_provisioning_capsule.id,
        template=module_provisioning_capsule.id,
        dns=module_provisioning_capsule.id,
        httpboot=module_provisioning_capsule.id,
        discovery=module_provisioning_capsule.id,
        remote_execution_proxy=[module_provisioning_capsule.id],
        domain=[domain.id],
    ).create()

    return Box(sat=sat, domain=domain, subnet=subnet, provisioning_type=provisioning_type)


@pytest.fixture(scope='module')
def module_ssh_key_file():
    _, layout = mkstemp(text=True)
    os.chmod(layout, 0o600)
    with open(layout, 'w') as ssh_key:
        ssh_key.write(settings.provisioning.host_ssh_key_priv)
    return layout


@pytest.fixture
def provisioning_host(module_ssh_key_file, pxe_loader, module_provisioning_sat):
    """Fixture to check out blank VM"""
    vlan_id = settings.provisioning.vlan_id
    cd_iso = (
        ""  # TODO: Make this an optional fixture parameter (update vm_firmware when adding this)
    )
    with Broker(
        workflow=settings.provisioning.provisioning_host_workflow,
        host_class=ContentHost,
        target_vlan_id=vlan_id,
        target_vm_firmware=pxe_loader.vm_firmware,
        target_pxeless_image=cd_iso,
        blank=True,
        target_memory='6GiB',
        auth=module_ssh_key_file,
    ) as prov_host:
        yield prov_host
        # Set host as non-blank to run teardown of the host
        assert module_provisioning_sat.sat.execute('systemctl restart dhcpd').status == 0
        prov_host.blank = getattr(prov_host, 'blank', False)


@pytest.fixture
def provision_multiple_hosts(module_ssh_key_file, pxe_loader, request):
    """Fixture to check out two blank VMs"""
    vlan_id = settings.provisioning.vlan_id
    cd_iso = (
        ""  # TODO: Make this an optional fixture parameter (update vm_firmware when adding this)
    )
    with Broker(
        workflow=settings.provisioning.provisioning_host_workflow,
        host_class=ContentHost,
        _count=getattr(request, 'param', 2),
        target_vlan_id=vlan_id,
        target_vm_firmware=pxe_loader.vm_firmware,
        target_pxeless_image=cd_iso,
        blank=True,
        target_memory='6GiB',
        auth=module_ssh_key_file,
    ) as hosts:
        yield hosts

        for prov_host in hosts:
            prov_host.blank = getattr(prov_host, 'blank', False)


@pytest.fixture
def provisioning_hostgroup(
    module_provisioning_sat,
    module_sca_manifest_org,
    module_location,
    default_architecture,
    module_provisioning_rhel_content,
    module_lce_library,
    default_partitiontable,
    module_provisioning_capsule,
    pxe_loader,
):
    return module_provisioning_sat.sat.api.HostGroup(
        organization=[module_sca_manifest_org],
        location=[module_location],
        architecture=default_architecture,
        domain=module_provisioning_sat.domain,
        content_source=module_provisioning_capsule.id,
        content_view=module_provisioning_rhel_content.cv,
        kickstart_repository=module_provisioning_rhel_content.ksrepo,
        lifecycle_environment=module_lce_library,
        root_pass=settings.provisioning.host_root_password,
        operatingsystem=module_provisioning_rhel_content.os,
        ptable=default_partitiontable,
        subnet=module_provisioning_sat.subnet,
        pxe_loader=pxe_loader.pxe_loader,
        group_parameters_attributes=[
            {
                'name': 'remote_execution_ssh_keys',
                'parameter_type': 'string',
                'value': settings.provisioning.host_ssh_key_pub,
            },
            # assign AK in order the hosts to be subscribed
            {
                'name': 'kt_activation_keys',
                'parameter_type': 'string',
                'value': module_provisioning_rhel_content.ak.name,
            },
        ],
    ).create()


@pytest.fixture
def pxe_loader(request):
    """Map the appropriate PXE loader to VM bootloader"""
    PXE_LOADER_MAP = {
        'bios': {'vm_firmware': 'bios', 'pxe_loader': 'PXELinux BIOS'},
        'uefi': {'vm_firmware': 'uefi', 'pxe_loader': 'Grub2 UEFI'},
        'ipxe': {'vm_firmware': 'bios', 'pxe_loader': 'iPXE Embedded'},
        'http_uefi': {'vm_firmware': 'uefi', 'pxe_loader': 'Grub2 UEFI HTTP'},
    }
    return Box(PXE_LOADER_MAP[getattr(request, 'param', 'bios')])


@pytest.fixture
def pxeless_discovery_host(provisioning_host, module_discovery_sat, pxe_loader):
    """Fixture for returning a pxe-less discovery host for provisioning"""
    sat = module_discovery_sat.sat
    image_name = f"{gen_string('alpha')}-{module_discovery_sat.iso}"
    mac = provisioning_host._broker_args['provisioning_nic_mac_addr']
    # Remaster and upload discovery image to automatically input values
    result = sat.execute(
        'cd /var/www/html/pub && '
        f'discovery-remaster {module_discovery_sat.iso} '
        f'"proxy.type=foreman proxy.url=https://{sat.hostname}:443 fdi.pxmac={mac} fdi.pxauto=1"'
    )
    pattern = re.compile(r"foreman-discovery-image\S+")
    fdi = pattern.findall(result.stdout)[0]
    Broker(
        workflow='import-disk-image',
        import_disk_image_name=image_name,
        import_disk_image_url=(f'https://{sat.hostname}/pub/{fdi}'),
        firmware_type=pxe_loader.vm_firmware,
    ).execute()
    # Change host to boot discovery image
    Broker(
        job_template='configure-pxe-boot',
        target_host=provisioning_host.name,
        target_vlan_id=settings.provisioning.vlan_id,
        target_vm_firmware=provisioning_host._broker_args['target_vm_firmware'],
        target_pxeless_image=image_name,
        target_boot_scenario='pxeless_pre',
    ).execute()
    yield provisioning_host
    Broker(workflow='remove-disk-image', remove_disk_image_name=image_name).execute()
