# Tauri NSIS template provenance

- Upstream tag: `tauri-cli-v2.11.4`
- Upstream file: <https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.11.4/crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi>
- Upstream SHA-256: `20f4ecc730defb71f1342eaeaec4021df13be3d843abba0effe88ea5835fa079`
- Locally patched SHA-256: `8cb7bffce6d79e3d20cbcac62c59aad22d0db033b4c82c2ac8ec78e4d7385f60`

The only local change keeps current-user program files separate from Stock Desk user data:

```diff
-      StrCpy $INSTDIR "$LOCALAPPDATA\${PRODUCTNAME}"
+      StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PRODUCTNAME}"
```
