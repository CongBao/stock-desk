# Tauri NSIS template provenance

- Upstream tag: `tauri-cli-v2.11.4`
- Upstream file: <https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.11.4/crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi>
- Upstream SHA-256: `20f4ecc730defb71f1342eaeaec4021df13be3d843abba0effe88ea5835fa079`
- Locally patched SHA-256: `0dc615212e37369b747a4916d7a4de53533ec3e2552ab55759effbb8193cae44`

The local changes keep current-user program files separate from Stock Desk user data
and exclude checkout mtimes from the independently reproduced installer payload:

```diff
-      StrCpy $INSTDIR "$LOCALAPPDATA\${PRODUCTNAME}"
+      StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PRODUCTNAME}"
```

```diff
+; Independent CI runners check out identical bytes with different mtimes.
+; Do not serialize those host timestamps into the otherwise identical payload.
+SetDateSave off
```

Stock Desk-specific uninstall behavior is intentionally kept out of the
vendored template. `installer-hooks.nsh` uses Tauri's supported NSIS hook
surface to copy the installed host to the NSIS plug-in directory and invoke
its fixed v1.1 cleanup mode only after an explicit, default-off user choice.
The English and Simplified Chinese custom language files are derived from the
same `tauri-cli-v2.11.4` language files and change only the data-removal
description plus the fail-closed retry/keep-data explanation.
