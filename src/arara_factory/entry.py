from __future__ import annotations

import ctypes
import os


_SINGLE_INSTANCE_HANDLE = None


def _acquire_single_instance() -> bool:
    global _SINGLE_INSTANCE_HANDLE
    if os.name != "nt":
        return True

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\ARARA_Factory_Single_Instance")
    if not handle:
        return True
    _SINGLE_INSTANCE_HANDLE = handle
    already_exists = kernel32.GetLastError() == 183
    if already_exists:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Verticliv уже открыта. Используй текущее окно программы.",
            "Verticliv",
            0x40,
        )
        return False
    return True


def main() -> None:
    if not _acquire_single_instance():
        return

    from . import app as app_module
    from .integrated_batch import install as install_batch
    from .publishing_ui import install as install_publishing
    from .publishing_oauth_ui import install as install_publishing_oauth
    from .publishing_runtime_ui import install as install_publishing_runtime
    from .publishing_library_ui import install as install_publishing_library
    from .smart_ui import install as install_smart_ui
    from .publishing_targets_ui import install as install_publishing_targets
    from .publishing_reliable_ui import install as install_publishing_reliable

    install_batch(app_module)
    install_publishing(app_module)
    install_publishing_oauth(app_module)
    install_publishing_runtime(app_module)
    install_publishing_library(app_module)
    install_smart_ui(app_module)
    install_publishing_targets(app_module)
    install_publishing_reliable(app_module)
    # Preserve the full legacy workflow as a secondary window.
    from .studio_ui import StudioWindow
    legacy_window = app_module.MainWindow
    app_module.MainWindow = lambda: StudioWindow(legacy_factory=legacy_window)
    app_module.main()
