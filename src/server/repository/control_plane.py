"""Repository classes for the control plane. No ORM calls outside these classes."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import func, text
from sqlmodel import Session, select

from src.shared.utils import utcnow
from src.server.models.control_plane import ApiKey, PasswordResetToken, PublicLibrary, RevokedToken, Tenant, TenantDbRouting, User
from ulid import ULID


# An account deleted by the server admin: kept, never reached (TenantRepository.gone).
DELETED = "deleted"
# The label of the key the scheduler makes for itself in each account (scheduler/service.py scheduler_key): what
# people see it as in the account's keys. What makes it the scheduler's is api_keys.scheduler, set only by
# ApiKeyRepository.new_scheduler_key: the one key an AI machine's key is ever handed to (ai_machines.key_for). No
# other key may be labelled so (scheduler_label), whatever its case, so none looks like it.
SCHEDULER_KEY_LABEL = "scheduler"
SCHEDULER_LABEL_TAKEN = "“scheduler” is the label of Lumiverb's own key: give this one another."


def scheduler_label(label: str | None) -> bool:
    """Whether a label is the scheduler's own key's, as people would read it (any case, spaces around it)."""
    return label is not None and label.strip().casefold() == SCHEDULER_KEY_LABEL


class TenantRepository:
    """Repository for tenants table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        name: str,
        plan: str = "free",
        vision_api_url: str = "",
        vision_api_key: str = "",
    ) -> Tenant:
        """Generate tenant_id as ten_ + ulid(), insert, return the new Tenant."""
        tenant_id = "ten_" + str(ULID())
        tenant = Tenant(
            tenant_id=tenant_id,
            name=name,
            plan=plan,
            status="active",
        )
        self._session.add(tenant)
        self._session.flush()
        from src.server.repository.ai_machines import new_built_in_machine, new_vision_machine

        # Its built-in Whisper, so transcripts run with nothing set up (routers/ai.py).
        self._session.add(new_built_in_machine(tenant_id))
        self._session.flush()
        # A vision endpoint given now is its first AI machine.
        if vision_api_url.strip():
            self._session.add(new_vision_machine(self._session, tenant_id, vision_api_url, vision_api_key))
        self._session.commit()
        self._session.refresh(tenant)
        return tenant

    def get_by_id(self, tenant_id: str) -> Tenant | None:
        """Return tenant by id or None."""
        stmt = select(Tenant).where(Tenant.tenant_id == tenant_id)
        return self._session.exec(stmt).first()

    def key_idle_days(self, tenant_id: str) -> int | None:
        """Days a key a person made may go unused before it ends; None while that's off."""
        tenant = self.get_by_id(tenant_id)
        return tenant.key_idle_days if tenant is not None else None

    def set_key_idle_days(self, tenant_id: str, days: int | None) -> None:
        tenant = self.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant not found: {tenant_id}")
        tenant.key_idle_days = days
        self._session.add(tenant)
        self._session.commit()

    def folders_of(self, tenant_id: str, *, hold: bool = False) -> list[str] | None:
        """The account's folders on this server, or None for every folder the
        server reads. hold: they can't change (FOR SHARE) until this session's
        transaction ends, so a library's folder is checked and saved against
        them as they are (src/server/api/account_folders.py)."""
        stmt = select(Tenant.folders).where(Tenant.tenant_id == tenant_id)
        if hold:
            stmt = stmt.with_for_update(read=True)
        return self._session.exec(stmt).first()

    def to_change_folders(self, tenant_id: str) -> Tenant | None:
        """The account, to change its folders: one change of any account's at
        a time (a transaction's advisory lock), and its row held (FOR NO KEY
        UPDATE) from a library's folder being chosen in it until the change
        is committed (set_folders) or rolled back."""
        self._session.execute(text("SELECT pg_advisory_xact_lock(hashtext('tenants.folders'))"))
        stmt = select(Tenant).where(Tenant.tenant_id == tenant_id).with_for_update(key_share=True)
        return self._session.exec(stmt).first()

    def set_folders(self, tenant: Tenant, folders: list[str] | None) -> None:
        tenant.folders = None if folders is None else list(folders)
        self._session.add(tenant)
        self._session.commit()

    def list_all(self) -> list[Tenant]:
        """Return all tenants."""
        stmt = select(Tenant)
        return list(self._session.exec(stmt).all())

    def list_live(self) -> list[Tenant]:
        """Every account but those deleted (the server admin's DELETE /v1/admin/tenants): what's worked on."""
        return [t for t in self.list_all() if t.status != DELETED]

    def gone(self, tenant_id: str) -> bool:
        """No such account, or a deleted one: nothing of it is reached, signed in, by a key or a public link."""
        tenant = self.get_by_id(tenant_id)
        return tenant is None or tenant.status == DELETED

    def rename(self, tenant_id: str, name: str) -> None:
        tenant = self.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant not found: {tenant_id}")
        tenant.name = name
        self._session.add(tenant)
        self._session.commit()

    def update_status(self, tenant_id: str, status: str) -> Tenant:
        """Update tenant status and return the tenant."""
        tenant = self.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant not found: {tenant_id}")
        tenant.status = status
        self._session.add(tenant)
        self._session.commit()
        self._session.refresh(tenant)
        return tenant

    def delete(self, tenant_id: str) -> None:
        """Delete tenant record (for cleanup on provisioning failure)."""
        tenant = self.get_by_id(tenant_id)
        if tenant is not None:
            self._session.delete(tenant)
            self._session.commit()


# A device's key's role: no person's (admin, editor, viewer), so no check that asks for one lets it by.
DEVICE_ROLE = "device"


class ApiKeyRepository:
    """Repository for api_keys table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _hash_key(plaintext: str) -> str:
        # Preserve existing SHA256 hashing scheme for compatibility.
        return hashlib.sha256(plaintext.encode()).hexdigest()

    @staticmethod
    def _generate_plaintext() -> str:
        """
        Generate plaintext key as lv_ + ULID() + random suffix.

        ULID prefix ensures sortable, non-guessable IDs while keeping the
        existing lv_ prefix for callers.
        """
        ulid_part = str(ULID())
        random_part = secrets.token_urlsafe(16)
        return f"lv_{ulid_part}_{random_part}"

    def create(
        self,
        tenant_id: str,
        label: str | None,
        role: str = "admin",
        created_by_user_id: str | None = None,
    ) -> tuple[ApiKey, str]:
        """
        Create a new API key for a tenant.

        Generates a new lv_ ULID-prefixed key, hashes it, inserts the row, and
        returns (record, plaintext). Plaintext is returned ONCE and never stored.
        Never labelled as the scheduler's own key (ValueError: the routes
        refuse it first, 400 scheduler_label): that's new_scheduler_key's.
        """
        if scheduler_label(label):
            raise ValueError(SCHEDULER_LABEL_TAKEN)
        plaintext = self._generate_plaintext()
        key_hash = self._hash_key(plaintext)
        key_id = "key_" + str(ULID())
        api_key = ApiKey(
            key_id=key_id,
            key_hash=key_hash,
            tenant_id=tenant_id,
            name=label or "default",
            label=label,
            scopes=["read", "write"],
            role=role,
            created_by_user_id=created_by_user_id,
        )
        self._session.add(api_key)
        self._session.commit()
        self._session.refresh(api_key)
        return api_key, plaintext

    def get(self, key_id: str) -> ApiKey | None:
        return self._session.get(ApiKey, key_id)

    def get_for_tenant(self, key_id: str, tenant_id: str) -> ApiKey | None:
        """The account's key with this id (revoked or not); never a device's (device_keys)."""
        return self._session.exec(select(ApiKey).where(ApiKey.key_id == key_id, ApiKey.tenant_id == tenant_id,
                                                       ApiKey.device_id.is_(None))).first()

    def get_by_hash(self, key_hash: str) -> ApiKey | None:
        """Return API key by hash, excluding revoked."""
        stmt = (
            select(ApiKey)
            .where(ApiKey.key_hash == key_hash)
            .where(ApiKey.revoked_at.is_(None))
        )
        return self._session.exec(stmt).first()

    def get_by_plaintext(self, plaintext: str) -> ApiKey | None:
        """Hash the plaintext and call get_by_hash."""
        return self.get_by_hash(self._hash_key(plaintext))

    def revoke(self, key_id: str, tenant_id: str | None = None) -> bool:
        """
        Set revoked_at for the given key_id (and optional tenant_id).

        Returns False if not found or already revoked; True on successful revoke.
        """
        stmt = select(ApiKey).where(ApiKey.key_id == key_id)
        if tenant_id is not None:
            stmt = stmt.where(ApiKey.tenant_id == tenant_id)
        api_key = self._session.exec(stmt).first()
        if api_key is None or api_key.revoked_at is not None:
            return False
        api_key.revoked_at = utcnow()
        self._session.add(api_key)
        self._session.commit()
        self._session.refresh(api_key)
        return True

    def live_made_by(self, tenant_id: str, user_id: str, roles: set[str] | None = None) -> list[ApiKey]:
        """The live keys a user minted (only those with one of ``roles``, if
        given), devices' keys included: what a new password of theirs ends."""
        stmt = (
            select(ApiKey)
            .where(ApiKey.tenant_id == tenant_id)
            .where(ApiKey.created_by_user_id == user_id)
            .where(ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at)
        )
        if roles is not None:
            stmt = stmt.where(ApiKey.role.in_(roles))
        return list(self._session.exec(stmt).all())

    def revoke_created_by(self, tenant_id: str, user_id: str, roles: set[str] | None = None) -> int:
        """Revoke the live keys a user minted (only those with one of
        ``roles``, if given). Uncommitted: it goes with the caller's change
        to the user. Returns how many."""
        keys = self.live_made_by(tenant_id, user_id, roles)
        for api_key in keys:
            api_key.revoked_at = utcnow()
            self._session.add(api_key)
        return len(keys)

    def revoke_all(self, tenant_id: str) -> int:
        """Revoke every live key of the account, devices' too. Uncommitted: it
        goes with the caller's change to the account. Returns how many."""
        keys = self._session.exec(select(ApiKey).where(
            ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None))).all()
        now = utcnow()
        for api_key in keys:
            api_key.revoked_at = now
            self._session.add(api_key)
        return len(keys)

    def new_device_key(self, tenant_id: str, device_id: str, created_by_user_id: str | None) -> tuple[ApiKey, str]:
        """A new key for the device, and its old one (if any) revoked in the
        same commit: a device has one key. Its role is "device", which is no
        person's (src/server/api/devices.py). Like any key a user minted, a
        new password of theirs ends it. Returns (record, plaintext); the
        plaintext once, never stored."""
        now = utcnow()
        for old in self._session.exec(select(ApiKey).where(
                ApiKey.tenant_id == tenant_id, ApiKey.device_id == device_id, ApiKey.revoked_at.is_(None))).all():
            old.revoked_at = now
            self._session.add(old)
        self._session.flush()
        plaintext = self._generate_plaintext()
        api_key = ApiKey(key_id="key_" + str(ULID()), key_hash=self._hash_key(plaintext), tenant_id=tenant_id,
                         name="device", label=None, scopes=["scan"], role=DEVICE_ROLE,
                         created_by_user_id=created_by_user_id, device_id=device_id)
        self._session.add(api_key)
        self._session.commit()
        self._session.refresh(api_key)
        return api_key, plaintext

    def new_scheduler_key(self, tenant_id: str) -> tuple[ApiKey, str]:
        """A new key for the scheduler's calls on the account's behalf
        (scheduler/service.py scheduler_key), marked as its own
        (api_keys.scheduler: what the middleware reads, never the label), and
        made by no person. The account's earlier ones are revoked in the same
        commit, with any key still labelled as one from before the label was
        kept for it. Returns (record, plaintext); the plaintext once, never
        stored."""
        now = utcnow()
        for old in self._session.exec(select(ApiKey).where(
                ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None),
                ApiKey.scheduler | (ApiKey.label == SCHEDULER_KEY_LABEL))).all():
            old.revoked_at = now
            self._session.add(old)
        self._session.flush()
        plaintext = self._generate_plaintext()
        api_key = ApiKey(key_id="key_" + str(ULID()), key_hash=self._hash_key(plaintext), tenant_id=tenant_id,
                         name=SCHEDULER_KEY_LABEL, label=SCHEDULER_KEY_LABEL, scopes=["read", "write"], role="admin",
                         scheduler=True)
        self._session.add(api_key)
        self._session.commit()
        self._session.refresh(api_key)
        return api_key, plaintext

    def device_keys(self, tenant_id: str) -> dict[str, ApiKey]:
        """Each of the account's devices' live key, by device."""
        return {k.device_id: k for k in self._session.exec(select(ApiKey).where(
            ApiKey.tenant_id == tenant_id, ApiKey.device_id.is_not(None), ApiKey.revoked_at.is_(None))).all()}

    def end_device_key(self, tenant_id: str, device_id: str) -> bool:
        """Revoke the device's key. False when it has none."""
        keys = self._session.exec(select(ApiKey).where(
            ApiKey.tenant_id == tenant_id, ApiKey.device_id == device_id, ApiKey.revoked_at.is_(None))).all()
        for api_key in keys:
            api_key.revoked_at = utcnow()
            self._session.add(api_key)
        self._session.commit()
        return bool(keys)

    def touch_last_used(self, key_id: str) -> None:
        """Update last_used_at. Best-effort; do not raise on failure."""
        stmt = select(ApiKey).where(ApiKey.key_id == key_id)
        api_key = self._session.exec(stmt).first()
        if api_key is not None:
            api_key.last_used_at = utcnow()
            self._session.add(api_key)
            try:
                self._session.commit()
            except Exception:
                self._session.rollback()

    def list_by_tenant(self, tenant_id: str) -> list[ApiKey]:
        """
        List all non-revoked API keys for a tenant, ordered by created_at ASC:
        people's and the account's, never a device's (device_keys).

        Plaintext is never returned; only metadata.
        """
        stmt = (
            select(ApiKey)
            .where(ApiKey.tenant_id == tenant_id)
            .where(ApiKey.revoked_at.is_(None))
            .where(ApiKey.device_id.is_(None))
            .order_by(ApiKey.created_at.asc())
        )
        return list(self._session.exec(stmt).all())

    def list_by_tenant_id(self, tenant_id: str) -> list[ApiKey]:
        """List all API keys for a tenant (including revoked)."""
        stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_id)
        return list(self._session.exec(stmt).all())

    def count_admin_keys(self, tenant_id: str) -> int:
        """Return the number of non-revoked admin keys for a tenant."""
        stmt = (
            select(ApiKey)
            .where(ApiKey.tenant_id == tenant_id)
            .where(ApiKey.revoked_at.is_(None))
            .where(ApiKey.role == "admin")
        )
        return len(list(self._session.exec(stmt).all()))

    @staticmethod
    def ends_unused(api_key: ApiKey) -> bool:
        """Whether the account's key-idle days end this key: one a person made.
        Never one nobody made (the scheduler's, the server admin's API's: the
        install's own) or a device's (the Devices API's to end)."""
        return api_key.created_by_user_id is not None and api_key.device_id is None

    def unused_since(self, tenant_id: str, since: datetime, *, lock: bool = False) -> list[ApiKey]:
        """The account's live keys a person made that nobody has used since
        `since` (made before it, if never used), longest unused first, but
        for the account's last admin key: it's kept (DELETE /v1/keys keeps
        it too)."""
        stmt = (
            select(ApiKey)
            .where(ApiKey.tenant_id == tenant_id)
            .where(ApiKey.revoked_at.is_(None))
            .where(ApiKey.created_by_user_id.is_not(None))
            .where(ApiKey.device_id.is_(None))
            .where(func.coalesce(ApiKey.last_used_at, ApiKey.created_at) < since)
            .order_by(func.coalesce(ApiKey.last_used_at, ApiKey.created_at), ApiKey.key_id)
        )
        if lock:
            stmt = stmt.with_for_update()
        idle = list(self._session.exec(stmt).all())
        admins_left = self.count_admin_keys(tenant_id)
        ending = []
        for api_key in idle:
            if api_key.role == "admin":
                if admins_left <= 1:
                    continue
                admins_left -= 1
            ending.append(api_key)
        return ending

    def end_unused(self, tenant_id: str, since: datetime) -> int:
        """End the keys unused_since names, and commit. Returns how many."""
        ending = self.unused_since(tenant_id, since, lock=True)
        now = utcnow()
        for api_key in ending:
            api_key.revoked_at = now
            self._session.add(api_key)
        self._session.commit()
        return len(ending)

    def delete_by_tenant_id(self, tenant_id: str) -> None:
        """Delete all API keys for a tenant (for cleanup on provisioning failure)."""
        for key in self.list_by_tenant_id(tenant_id):
            self._session.delete(key)
        self._session.commit()


class TenantDbRoutingRepository:
    """Repository for tenant_db_routing table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        tenant_id: str,
        connection_string: str,
        region: str = "local",
    ) -> TenantDbRouting:
        """Create a routing entry for a tenant."""
        row = TenantDbRouting(
            tenant_id=tenant_id,
            connection_string=connection_string,
            region=region,
        )
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def get_by_tenant_id(self, tenant_id: str) -> TenantDbRouting | None:
        """Return routing entry by tenant_id or None."""
        stmt = select(TenantDbRouting).where(TenantDbRouting.tenant_id == tenant_id)
        return self._session.exec(stmt).first()

    def delete_by_tenant_id(self, tenant_id: str) -> None:
        """Delete routing entry for tenant (for cleanup on provisioning failure)."""
        row = self.get_by_tenant_id(tenant_id)
        if row is not None:
            self._session.delete(row)
            self._session.commit()


class UserRepository:
    """Repository for users table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, tenant_id: str, email: str, password_hash: str, role: str = "viewer", *,
               server_admin: bool = False, every_library: bool = True) -> User:
        from ulid import ULID
        user = User(
            user_id="usr_" + str(ULID()),
            tenant_id=tenant_id,
            email=email,
            password_hash=password_hash,
            role=role,
            server_admin=server_admin,
            every_library=every_library,
        )
        self._session.add(user)
        self._session.commit()
        self._session.refresh(user)
        return user

    def get_by_email(self, email: str) -> User | None:
        """Whoever has this address, whatever its case or the spaces around it (an address is one person's
        however it's typed: users_email_any_case). The first made, on an install from before that had two."""
        stmt = (select(User).where(func.lower(User.email) == email.strip().lower())
                .order_by(User.created_at, User.user_id))
        return self._session.exec(stmt).first()

    def get_by_id(self, user_id: str) -> User | None:
        return self._session.get(User, user_id)

    def list_by_tenant(self, tenant_id: str) -> list[User]:
        stmt = select(User).where(User.tenant_id == tenant_id).order_by(User.created_at.asc())
        return list(self._session.exec(stmt).all())

    def count_by_tenant(self) -> dict[str, int]:
        """How many people each account has, by its id."""
        rows = self._session.exec(select(User.tenant_id, func.count()).group_by(User.tenant_id)).all()
        return {tenant_id: int(n) for tenant_id, n in rows}

    def count_admins(self, tenant_id: str) -> int:
        stmt = select(User).where(User.tenant_id == tenant_id).where(User.role == "admin")
        return len(list(self._session.exec(stmt).all()))

    def count_admins_locked(self, tenant_id: str) -> int:
        """Count admins with FOR UPDATE lock to serialize concurrent last-admin checks."""
        stmt = (
            select(User)
            .where(User.tenant_id == tenant_id)
            .where(User.role == "admin")
            .with_for_update()
        )
        return len(list(self._session.exec(stmt).all()))

    def server_admins(self) -> list[User]:
        """The install's server admins, every account's, by email."""
        return list(self._session.exec(select(User).where(User.server_admin).order_by(User.email)).all())

    def keeps_a_server_admin(self, *, without_user: str | None = None, without_tenant: str | None = None) -> bool:
        """Whether a server admin who can still sign in (one in an account
        not deleted) is left once that user, or that account, goes. The one
        rule for taking a server admin away, by removing them as one,
        deleting them or deleting their account. The server admins are
        locked (FOR UPDATE) until the caller commits or rolls back, so two
        such changes at once leave one; their accounts are read after the
        lock is had, as they are then."""
        admins = self._session.exec(
            select(User.user_id, User.tenant_id).where(User.server_admin).with_for_update()).all()
        tenants = {tenant_id for _, tenant_id in admins}
        live = set(self._session.exec(select(Tenant.tenant_id).where(
            Tenant.tenant_id.in_(tenants), Tenant.status != DELETED)).all()) if tenants else set()
        return any(user_id != without_user and tenant_id != without_tenant and tenant_id in live
                   for user_id, tenant_id in admins)

    def name_server_admin(self, user_id: str) -> int:
        """Make the user a server admin, and commit. Their links not yet used
        (an invite, a reset) end with it: one an account admin made before
        would set a server admin's password, and only a server admin gives
        a server admin a link now. Returns how many still good ones ended;
        none for someone who's one already (nothing changes)."""
        user = self._session.exec(select(User).where(User.user_id == user_id).with_for_update()).first()
        if user is None:
            raise ValueError(f"User not found: {user_id}")
        if user.server_admin:
            self._session.rollback()
            return 0
        user.server_admin = True
        self._session.add(user)
        ended = PasswordResetTokenRepository(self._session).void_unused(user_id)
        self._session.commit()
        return ended

    def set_server_admin(self, user_id: str, server_admin: bool, *, keep_one: bool = False) -> bool:
        """Make the user a server admin (name_server_admin) or not, and
        commit. False when keep_one and they're the last one who can sign in
        (nothing changes): keeps_a_server_admin, so two taking each other's
        away at once leave one."""
        if server_admin:
            self.name_server_admin(user_id)
            return True
        keeps = keep_one and self.keeps_a_server_admin(without_user=user_id)
        user = self._session.exec(select(User).where(User.user_id == user_id).with_for_update()).first()
        if user is None:
            raise ValueError(f"User not found: {user_id}")
        if keep_one and not server_admin and user.server_admin and not keeps:
            self._session.rollback()
            return False
        user.server_admin = server_admin
        self._session.add(user)
        self._session.commit()
        return True

    def update_role(self, user_id: str, role: str) -> User:
        user = self.get_by_id(user_id)
        if user is None:
            raise ValueError(f"User not found: {user_id}")
        if user.role != role:
            user.role = role
            user.token_version += 1  # tokens carry the role: revoke them
        self._session.add(user)
        self._session.commit()
        self._session.refresh(user)
        return user

    def set_every_library(self, user_id: str, every_library: bool) -> User:
        """Whether they see every library (their account role on each) or
        only the ones they're given a role on; read at each request, so it
        takes effect at once."""
        user = self.get_by_id(user_id)
        if user is None:
            raise ValueError(f"User not found: {user_id}")
        user.every_library = every_library
        self._session.add(user)
        self._session.commit()
        self._session.refresh(user)
        return user

    def update_last_login(self, user_id: str) -> None:
        user = self.get_by_id(user_id)
        if user is not None:
            user.last_login_at = utcnow()
            self._session.add(user)
            try:
                self._session.commit()
            except Exception:
                self._session.rollback()

    def update_password(self, user_id: str, password_hash: str) -> int:
        """A new password (the operator's `lumiverb-server reset-password`),
        ending what the old one could have made. Returns how many of the
        user's API keys it revoked."""
        user = self._session.exec(select(User).where(User.user_id == user_id).with_for_update()).first()
        if user is None:
            raise ValueError(f"User not found: {user_id}")
        user.password_hash = password_hash
        revoked = _end_what_the_old_password_made(self._session, user)
        self._session.commit()
        return revoked

    def delete(self, user_id: str) -> None:
        """The user, with their reset and invite links, used or not: each names them, so none may outlive them."""
        user = self.get_by_id(user_id)
        if user is not None:
            for token in self._session.exec(select(PasswordResetToken).where(
                    PasswordResetToken.user_id == user_id)).all():
                self._session.delete(token)
            self._session.flush()
            self._session.delete(user)
            self._session.commit()


class PasswordResetTokenRepository:
    """Repository for password_reset_tokens table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _hash_token(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode()).hexdigest()

    def create(self, user_id: str, plaintext_token: str, expires_at: datetime) -> PasswordResetToken:
        token = PasswordResetToken(
            token_hash=self._hash_token(plaintext_token),
            user_id=user_id,
            expires_at=expires_at,
        )
        self._session.add(token)
        self._session.commit()
        self._session.refresh(token)
        return token

    def get_by_plaintext(self, plaintext_token: str) -> PasswordResetToken | None:
        token_hash = self._hash_token(plaintext_token)
        return self._session.get(PasswordResetToken, token_hash)

    def mark_used(self, token_hash: str) -> None:
        token = self._session.get(PasswordResetToken, token_hash)
        if token is not None:
            token.used_at = utcnow()
            self._session.add(token)
            self._session.commit()

    def void_unused(self, user_id: str) -> int:
        """Every link the user hasn't used stops working (a new one, or a new
        password, replaces them). Uncommitted: it goes with what replaces them.
        Returns how many of them still worked (not expired)."""
        now = utcnow()
        stmt = (
            select(PasswordResetToken)
            .where(PasswordResetToken.user_id == user_id)
            .where(PasswordResetToken.used_at.is_(None))
        )
        working = 0
        for token in self._session.exec(stmt).all():
            working += token.expires_at > now
            token.used_at = now
            self._session.add(token)
        return working

    def consume_valid_and_update_password(self, plaintext_token: str, password_hash: str) -> tuple[User, int] | None:
        """
        Atomically consume a valid reset token and update the user's password,
        ending what the old one could have made.

        Returns the user and how many of their API keys were revoked when the
        token was valid and consumed; None when invalid, expired, or already used.
        """
        token_hash = self._hash_token(plaintext_token)
        user_id = self._session.exec(
            select(PasswordResetToken.user_id).where(PasswordResetToken.token_hash == token_hash)
        ).first()
        if user_id is None:
            return None
        # The user first, then the link: of two of their links used at once,
        # one sets the password and the other finds itself void.
        user = self._session.exec(select(User).where(User.user_id == user_id).with_for_update()).first()
        if user is None:
            self._session.rollback()
            return None

        now = utcnow()
        stmt = (
            select(PasswordResetToken)
            .where(PasswordResetToken.token_hash == token_hash)
            .where(PasswordResetToken.used_at.is_(None))
            .where(PasswordResetToken.expires_at > now)
            .with_for_update()
        )
        token = self._session.exec(stmt).first()
        if token is None:
            self._session.rollback()
            return None

        user.password_hash = password_hash
        token.used_at = now
        self._session.add(token)
        revoked = _end_what_the_old_password_made(self._session, user)
        self._session.commit()
        return user, revoked


def _end_what_the_old_password_made(session: Session, user: User) -> int:
    """A new password ends everything the old one could have made: every
    session (the token version moves on), the API keys the user made (and
    keys made with those), and their links not yet used, reset and invite
    links alike. Keys nobody made (the scheduler's, the operator's) stay.
    Uncommitted: it goes with the new password. Returns how many keys."""
    user.token_version += 1
    session.add(user)
    PasswordResetTokenRepository(session).void_unused(user.user_id)
    return ApiKeyRepository(session).revoke_created_by(user.tenant_id, user.user_id)


class PublicLibraryRepository:
    """Repository for public_libraries control plane table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, library_id: str) -> PublicLibrary | None:
        return self._session.get(PublicLibrary, library_id)

    def upsert(self, library_id: str, tenant_id: str) -> None:
        row = PublicLibrary(library_id=library_id, tenant_id=tenant_id)
        self._session.merge(row)
        self._session.commit()

    def delete(self, library_id: str) -> None:
        row = self._session.get(PublicLibrary, library_id)
        if row:
            self._session.delete(row)
            self._session.commit()


class PublicProjectRepository:
    """Repository for public_projects control plane table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, project_id: str) -> "PublicProject | None":
        from src.server.models.control_plane import PublicProject
        return self._session.get(PublicProject, project_id)

    def upsert(self, project_id: str, tenant_id: str) -> None:
        from src.server.models.control_plane import PublicProject
        row = PublicProject(project_id=project_id, tenant_id=tenant_id)
        self._session.merge(row)
        self._session.commit()

    def delete(self, project_id: str) -> None:
        from src.server.models.control_plane import PublicProject
        row = self._session.get(PublicProject, project_id)
        if row:
            self._session.delete(row)
            self._session.commit()


class RevokedTokenRepository:
    """Repository for JWT revocation (logout / token refresh)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def revoke(self, jti: str) -> bool:
        """Mark a JWT as revoked. True when this call revoked it, False when it
        already was (so two refreshes of one token can't both succeed)."""
        from sqlalchemy import text

        result = self._session.execute(
            text("INSERT INTO revoked_tokens (jti, revoked_at) VALUES (:jti, now()) ON CONFLICT (jti) DO NOTHING"),
            {"jti": jti},
        )
        self._session.commit()
        return bool(result.rowcount)

    def is_revoked(self, jti: str) -> bool:
        return self._session.get(RevokedToken, jti) is not None

    def cleanup_expired(self, before: "datetime") -> int:
        """Delete revoked tokens older than the given timestamp (housekeeping)."""
        from sqlalchemy import text
        result = self._session.execute(
            text("DELETE FROM revoked_tokens WHERE revoked_at < :cutoff"),
            {"cutoff": before},
        )
        self._session.commit()
        return result.rowcount
