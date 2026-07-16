# Tauri NSIS template provenance

- Upstream tag: `tauri-cli-v2.11.4`
- Upstream file: <https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.11.4/crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi>
- Upstream SHA-256: `20f4ecc730defb71f1342eaeaec4021df13be3d843abba0effe88ea5835fa079`
- Locally patched SHA-256: `38dcdb560d91bdb544f2788aec051dcf57e78d7c5f8f3b122564fbf7e0e45d81`

The local changes keep current-user program files separate from Stock Desk user data
and exclude checkout metadata from the independently reproduced installer payload:

```diff
-      StrCpy $INSTDIR "$LOCALAPPDATA\${PRODUCTNAME}"
+      StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PRODUCTNAME}"
```

```diff
+; Independent CI runners check out identical bytes with different mtimes.
+; Do not serialize those host timestamps into the otherwise identical payload.
+SetDateSave off
```

```diff
-    File /a "/oname={{this.[1]}}" "{{no-escape @key}}"
+    File "/oname={{this.[1]}}" "{{no-escape @key}}"
-    File /a "/oname={{this}}" "{{no-escape @key}}"
+    File "/oname={{this}}" "{{no-escape @key}}"
```

The `/a` switches are removed because NSIS serializes source Windows attributes
when they are present. The owner-only repack snapshot intentionally marks its
files read-only, so inheriting that private snapshot attribute would make an
otherwise content-identical installer differ from the original Tauri candidate.
Installed resources and sidecars use normal destination attributes instead.

```diff
+      {{#if webview2_bootstrapper_path}}
       !if "${INSTALLWEBVIEW2MODE}" == "embedBootstrapper"
         ...
       !endif
+      {{/if}}
```

The Handlebars guard omits Tauri's inactive embed-bootstrapper branch when the
locked `offlineInstaller` mode does not provide a bootstrapper path. This keeps
the rendered script free of an unbound compile-time `File` source; embed mode
still renders the unchanged upstream branch when that path is present.

```diff
+      !ifmacrodef NSIS_HOOK_PREVIOUS_INSTALL_UNINSTALL
+        !insertmacro NSIS_HOOK_PREVIOUS_INSTALL_UNINSTALL "$4"
+      !endif
       ReadRegStr $R1 SHCTX "${UNINSTKEY}" "UninstallString"
```

The previous beta.3 uninstaller runs before Tauri's normal preinstall hook.
This additional hook point lets Stock Desk clear beta.3's inherited read-only
payload attributes before that old uninstaller starts, so a normal interactive
upgrade can complete instead of failing before the new files are copied.

All Stock Desk-specific behavior remains implemented in `installer-hooks.nsh`;
the vendored template only exposes the additional early hook point above.
The hooks copy the installed host to the NSIS plug-in directory and invoke its
fixed v1.1 cleanup mode only after an explicit, default-off user choice.
The English and Simplified Chinese custom language files are derived from the
same `tauri-cli-v2.11.4` language files and change only the data-removal
description plus the fail-closed retry/keep-data explanation.
