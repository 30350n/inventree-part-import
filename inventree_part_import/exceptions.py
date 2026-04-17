class InvenTreePartImportError(Exception):
    pass


class InvenTreeObjectCreationError(InvenTreePartImportError):
    def __init__(self, object_type: type, message: str = "unknown error"):
        self.object_type = object_type
        self.message = message
        super().__init__(str(self))

    def __str__(self):
        return f"failed to create '{self.object_type.__name__}' object ({self.message})"


class SupplierError(InvenTreePartImportError):
    def __init__(self, supplier: str, message: str):
        self.supplier = supplier
        self.message = message
        super().__init__(f"[{supplier.upper()}] {message}")


class SupplierLoadError(SupplierError):
    def __str__(self):
        return f"failed to load '{self.supplier}' supplier module ({self.message})"
