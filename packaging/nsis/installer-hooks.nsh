; Product-specific uninstall behavior belongs in supported Tauri NSIS hooks.

Var StockDeskCleanupReady
Var StockDeskCleanupExitCode
Var StockDeskWebView2Version
Var StockDeskWebView2Index
Var StockDeskWebView2Length
Var StockDeskWebView2Character
Var StockDeskWebView2Dots
Var StockDeskWebView2SegmentDigits
Var StockDeskAttributeScratch

; beta.3 inherited read-only attributes from the private repack snapshot. Clear
; only that bit from the existing program tree before an upgrade or uninstall.
; Reparse points are normalized but never traversed, so recursion cannot escape
; the caller-provided $INSTDIR. System.dll is already part of the locked NSIS
; toolchain and does not create a console window.
!macro StockDeskDefineClearLegacyReadOnlyAttributes FunctionName
Function ${FunctionName}
  Exch $0
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5

  System::Call 'kernel32::GetFileAttributesW(w r0)i.r4'
  ${If} $4 <> -1
    IntOp $5 $4 & 0x400
    IntOp $4 $4 & 0xFFFFFFFE
    System::Call 'kernel32::SetFileAttributesW(w r0, i r4)i'

    ${If} $5 = 0
      ClearErrors
      FindFirst $1 $2 "$0\*"
      ${IfNot} ${Errors}
        stock_desk_clear_legacy_readonly_loop:
          StrCmp $2 "" stock_desk_clear_legacy_readonly_done
          StrCmp $2 "." stock_desk_clear_legacy_readonly_next
          StrCmp $2 ".." stock_desk_clear_legacy_readonly_next
          StrCpy $3 "$0\$2"
          Push $3
          Call ${FunctionName}
          Pop $3

        stock_desk_clear_legacy_readonly_next:
          FindNext $1 $2
          Goto stock_desk_clear_legacy_readonly_loop

        stock_desk_clear_legacy_readonly_done:
          FindClose $1
      ${EndIf}
    ${EndIf}
  ${EndIf}

  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Exch $0
FunctionEnd
!macroend

!insertmacro StockDeskDefineClearLegacyReadOnlyAttributes StockDeskClearLegacyReadOnlyAttributes
!insertmacro StockDeskDefineClearLegacyReadOnlyAttributes un.StockDeskClearLegacyReadOnlyAttributes

; Keep this check in the supported pre-install hook. Tauri's vendored minimum-
; version branch invokes EdgeUpdate with needsadmin=true and offers Ignore, so
; Stock Desk deliberately leaves that branch disabled and verifies the
; production Evergreen Runtime itself after the offline installer has run.
!define STOCK_DESK_WEBVIEW2_APP_GUID "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
!define STOCK_DESK_MINIMUM_WEBVIEW2_VERSION "120.0.2210.91"
!define STOCK_DESK_WEBVIEW2_VERIFY_EXIT_CODE 71

; The vendored reinstall page can invoke the previous uninstaller before the
; normal preinstall hook. Repair beta.3 payload attributes first so that old
; uninstaller can delete its own files.
!macro NSIS_HOOK_PREVIOUS_INSTALL_UNINSTALL InstallRoot
  Push "${InstallRoot}"
  Call StockDeskClearLegacyReadOnlyAttributes
  Pop $StockDeskAttributeScratch
!macroend

!macro NSIS_HOOK_PREINSTALL
  Push "$INSTDIR"
  Call StockDeskClearLegacyReadOnlyAttributes
  Pop $StockDeskAttributeScratch

  stock_desk_verify_webview2:
    StrCpy $StockDeskWebView2Version ""
    ${If} ${RunningX64}
      ReadRegStr $StockDeskWebView2Version HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${STOCK_DESK_WEBVIEW2_APP_GUID}" "pv"
    ${Else}
      ReadRegStr $StockDeskWebView2Version HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\${STOCK_DESK_WEBVIEW2_APP_GUID}" "pv"
    ${EndIf}
    ${If} $StockDeskWebView2Version == ""
      ReadRegStr $StockDeskWebView2Version HKCU "SOFTWARE\Microsoft\EdgeUpdate\Clients\${STOCK_DESK_WEBVIEW2_APP_GUID}" "pv"
    ${EndIf}

    ; Accept exactly four non-empty decimal components. VersionCompare alone is
    ; intentionally not trusted with missing, sentinel, or malformed registry
    ; values.
    StrLen $StockDeskWebView2Length $StockDeskWebView2Version
    StrCpy $StockDeskWebView2Index 0
    StrCpy $StockDeskWebView2Dots 0
    StrCpy $StockDeskWebView2SegmentDigits 0
    ${If} $StockDeskWebView2Length = 0
      Goto stock_desk_webview2_invalid
    ${EndIf}
    ${If} $StockDeskWebView2Version == "0.0.0.0"
      Goto stock_desk_webview2_invalid
    ${EndIf}

    stock_desk_webview2_version_loop:
      ${If} $StockDeskWebView2Index >= $StockDeskWebView2Length
        Goto stock_desk_webview2_version_end
      ${EndIf}
      StrCpy $StockDeskWebView2Character $StockDeskWebView2Version 1 $StockDeskWebView2Index
      ${If} $StockDeskWebView2Character == "."
        ${If} $StockDeskWebView2SegmentDigits = 0
          Goto stock_desk_webview2_invalid
        ${EndIf}
        IntOp $StockDeskWebView2Dots $StockDeskWebView2Dots + 1
        ${If} $StockDeskWebView2Dots > 3
          Goto stock_desk_webview2_invalid
        ${EndIf}
        StrCpy $StockDeskWebView2SegmentDigits 0
        Goto stock_desk_webview2_next_character
      ${EndIf}
      ${If} $StockDeskWebView2Character != "0"
      ${AndIf} $StockDeskWebView2Character != "1"
      ${AndIf} $StockDeskWebView2Character != "2"
      ${AndIf} $StockDeskWebView2Character != "3"
      ${AndIf} $StockDeskWebView2Character != "4"
      ${AndIf} $StockDeskWebView2Character != "5"
      ${AndIf} $StockDeskWebView2Character != "6"
      ${AndIf} $StockDeskWebView2Character != "7"
      ${AndIf} $StockDeskWebView2Character != "8"
      ${AndIf} $StockDeskWebView2Character != "9"
        Goto stock_desk_webview2_invalid
      ${EndIf}
      IntOp $StockDeskWebView2SegmentDigits $StockDeskWebView2SegmentDigits + 1
      ${If} $StockDeskWebView2SegmentDigits > 9
        Goto stock_desk_webview2_invalid
      ${EndIf}

    stock_desk_webview2_next_character:
      IntOp $StockDeskWebView2Index $StockDeskWebView2Index + 1
      Goto stock_desk_webview2_version_loop

    stock_desk_webview2_version_end:
      ${If} $StockDeskWebView2Dots != 3
      ${OrIf} $StockDeskWebView2SegmentDigits = 0
        Goto stock_desk_webview2_invalid
      ${EndIf}
      ${VersionCompare} "${STOCK_DESK_MINIMUM_WEBVIEW2_VERSION}" "$StockDeskWebView2Version" $R0
      ${If} $R0 = 1
        Goto stock_desk_webview2_invalid
      ${EndIf}
      Goto stock_desk_webview2_verified

    stock_desk_webview2_invalid:
      ; Silent/passive installs must be deterministic and must not wait for UI.
      ${If} ${Silent}
      ${OrIf} $PassiveMode = 1
        SetErrorLevel ${STOCK_DESK_WEBVIEW2_VERIFY_EXIT_CODE}
        Abort
      ${EndIf}
      MessageBox MB_RETRYCANCEL|MB_ICONSTOP "$(stockDeskWebView2VerificationFailed)" IDRETRY stock_desk_verify_webview2 IDCANCEL stock_desk_webview2_cancel

    stock_desk_webview2_cancel:
      SetErrorLevel ${STOCK_DESK_WEBVIEW2_VERIFY_EXIT_CODE}
      Abort

    stock_desk_webview2_verified:
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  Push "$INSTDIR"
  Call un.StockDeskClearLegacyReadOnlyAttributes
  Pop $StockDeskAttributeScratch

  StrCpy $StockDeskCleanupReady 0

  ; Updates must never delete user data. The normal silent uninstaller also keeps
  ; data because Tauri's confirmation page (and its unchecked box) is skipped.
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    ClearErrors
    CopyFiles /SILENT "$INSTDIR\${MAINBINARYNAME}.exe" "$PLUGINSDIR\stock-desk-cleanup.exe"
    ${IfNot} ${Errors}
      IfFileExists "$PLUGINSDIR\stock-desk-cleanup.exe" 0 +2
        StrCpy $StockDeskCleanupReady 1
    ${EndIf}
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    ${If} $StockDeskCleanupReady <> 1
      ${IfNot} ${Silent}
      ${AndIf} $PassiveMode <> 1
        MessageBox MB_OK|MB_ICONINFORMATION "$(stockDeskCleanupUnavailable)"
      ${EndIf}
      Goto stock_desk_cleanup_done
    ${EndIf}

    ; ExecWait can fail before assigning its output variable. Initialize a
    ; stable, path-free sentinel so diagnostics never reuse an old value.
    StrCpy $StockDeskCleanupExitCode 70
    ClearErrors
    ExecWait '"$PLUGINSDIR\stock-desk-cleanup.exe" --stock-desk-uninstall-v11-data' $StockDeskCleanupExitCode
    ${IfNot} ${Errors}
    ${AndIf} $StockDeskCleanupExitCode = 0
      Goto stock_desk_cleanup_done
    ${EndIf}

    ; Optional data cleanup must never turn an otherwise successful uninstall
    ; into an error loop. Keep all remaining data and record only the stable,
    ; path-free helper exit code for diagnostics.
    DetailPrint "Stock Desk data cleanup exit code: $StockDeskCleanupExitCode"
    ${IfNot} ${Silent}
    ${AndIf} $PassiveMode <> 1
      MessageBox MB_OK|MB_ICONINFORMATION "$(stockDeskCleanupKeptData)"
    ${EndIf}

    stock_desk_cleanup_done:
  ${EndIf}
!macroend
