from __future__ import annotations


class LegacyAuthorityStoreError(ValueError):
    pass


class ReceiptConflictError(LegacyAuthorityStoreError):
    pass


class StoreQuarantinedError(LegacyAuthorityStoreError):
    pass
