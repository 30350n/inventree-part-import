class InvenTreePartImportError(Exception):
    pass


class InvenTreeObjectCreationError(InvenTreePartImportError):
    def __init__(self, object_type: type, message: str = "unknown error") -> None:
        super().__init__(f"Failed to create '{object_type.__name__}' object ({message})")


class SupplierError(InvenTreePartImportError):
    def __init__(self, supplier: str, message: str) -> None:
        self.supplier = supplier
        self.message = message
        super().__init__(f"[{supplier.upper()}] {message}")


class SupplierLoadError(SupplierError):
    pass
