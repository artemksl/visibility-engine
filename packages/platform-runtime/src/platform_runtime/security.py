"""Private local-file ownership through POSIX permissions or Windows ACLs."""
import os
from pathlib import Path
import stat
import sys

from .native_errors import windows_errors


def _windows_security(path, fd=None):
    import win32security
    from .processes import current_user_key
    flags = win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
    with windows_errors(filename=str(path)):
        if fd is None:
            descriptor = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT, flags)
        else:
            import msvcrt
            descriptor = win32security.GetSecurityInfo(msvcrt.get_osfhandle(fd), win32security.SE_FILE_OBJECT, flags)
        owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
        return descriptor, owner, current_user_key()


def is_owned_private(path, *, directory=False, fd=None):
    path = Path(path)
    info = path.lstat()
    if fd is not None:
        opened = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            return False
        info = opened
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        return False
    if sys.platform != "win32":
        return info.st_uid == os.getuid() and not info.st_mode & 0o077
    if getattr(info, "st_file_attributes", 0) & 0x400:  # reparse point, including junctions
        return False
    descriptor, owner, current = _windows_security(path, fd)
    if owner != current:
        return False
    with windows_errors(filename=str(path)):
        acl = descriptor.GetSecurityDescriptorDacl()
        if acl is None:
            return False
        permitted = {current, "S-1-5-18", "S-1-5-32-544"}  # account, SYSTEM, Administrators
        import win32security
        for index in range(acl.GetAceCount()):
            ace = acl.GetAce(index)
            kind = ace[0][0]
            if kind == win32security.ACCESS_ALLOWED_ACE_TYPE:
                if ace[1] and win32security.ConvertSidToStringSid(ace[2]) not in permitted:
                    return False
            elif kind != win32security.ACCESS_DENIED_ACE_TYPE:
                return False  # unsupported conditional/object ACE is not proof
    return True


def protect_private(path, *, directory=False):
    path = Path(path)
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise PermissionError("private resource is not the expected file type")
    if sys.platform != "win32":
        if info.st_uid != os.getuid():
            raise PermissionError("private resource belongs to another user")
        path.chmod(0o700 if directory else 0o600)
        return
    if getattr(info, "st_file_attributes", 0) & 0x400:
        raise PermissionError("private resources cannot be reparse points")
    descriptor, owner, current = _windows_security(path)
    if owner != current:
        raise PermissionError("private resource belongs to another user")
    import win32security
    with windows_errors(filename=str(path)):
        sid = win32security.ConvertStringSidToSid(current)
        acl = win32security.ACL()
        inheritance = 3 if directory else 0  # OBJECT_INHERIT | CONTAINER_INHERIT
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inheritance, 0x1f01ff, sid)
        win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
    if not is_owned_private(path, directory=directory):
        raise PermissionError("private resource ACL could not be verified")


def open_no_follow(path, flags, mode=0o600):
    """Open the final entry itself; callers validate its type and ownership."""
    if sys.platform != "win32":
        return os.open(path, flags | os.O_NOFOLLOW, mode)
    import msvcrt
    import win32file
    import win32con
    access = (win32con.GENERIC_READ | win32con.GENERIC_WRITE if flags & os.O_RDWR
              else win32con.GENERIC_WRITE if flags & os.O_WRONLY else win32con.GENERIC_READ)
    creation = (win32con.CREATE_NEW if flags & os.O_CREAT and flags & os.O_EXCL
                else win32con.CREATE_ALWAYS if flags & os.O_CREAT and flags & os.O_TRUNC
                else win32con.OPEN_ALWAYS if flags & os.O_CREAT
                else win32con.TRUNCATE_EXISTING if flags & os.O_TRUNC else win32con.OPEN_EXISTING)
    attributes = win32con.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_BACKUP_SEMANTICS
    with windows_errors(filename=str(path)):
        handle = win32file.CreateFile(str(path), access, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
                                     None, creation, attributes, None)
        try:
            raw = handle.Detach()
            try:
                return msvcrt.open_osfhandle(raw, flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR) | os.O_BINARY)
            except BaseException:
                import win32api
                win32api.CloseHandle(raw)
                raise
        finally:
            handle.Close()


def private_directory(path):
    path = Path(path)
    if path != path.resolve():
        raise ValueError("private directory cannot traverse a link")
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created:
        protect_private(path, directory=True)
    if not is_owned_private(path, directory=True):
        raise ValueError("directory is not owned and private")
