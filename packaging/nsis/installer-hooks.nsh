; Product-specific uninstall behavior belongs in supported Tauri NSIS hooks.

Var StockDeskCleanupReady
Var StockDeskCleanupExitCode
Var StockDeskWebView2Version
Var StockDeskWebView2Index
Var StockDeskWebView2Length
Var StockDeskWebView2Character
Var StockDeskWebView2Dots
Var StockDeskWebView2SegmentDigits

; Keep this check in the supported pre-install hook. Tauri's vendored minimum-
; version branch invokes EdgeUpdate with needsadmin=true and offers Ignore, so
; Stock Desk deliberately leaves that branch disabled and verifies the
; production Evergreen Runtime itself after the offline installer has run.
!define STOCK_DESK_WEBVIEW2_APP_GUID "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
!define STOCK_DESK_MINIMUM_WEBVIEW2_VERSION "120.0.2210.91"
!define STOCK_DESK_WEBVIEW2_VERIFY_EXIT_CODE 71

!macro NSIS_HOOK_PREINSTALL
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
      ${If} ${Silent}
      ${OrIf} $PassiveMode = 1
        SetErrorLevel 70
        Goto stock_desk_cleanup_done
      ${EndIf}
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(stockDeskCleanupUnavailable)"
      Goto stock_desk_cleanup_done
    ${EndIf}

    stock_desk_cleanup_retry:
      ClearErrors
      ExecWait '"$PLUGINSDIR\stock-desk-cleanup.exe" --stock-desk-uninstall-v11-data' $StockDeskCleanupExitCode
      ${IfNot} ${Errors}
      ${AndIf} $StockDeskCleanupExitCode = 0
        Goto stock_desk_cleanup_done
      ${EndIf}

      ; Never fall back to NSIS recursive deletion. The Rust cleanup mode owns
      ; the fixed Known Folder path and rejects reparse points fail closed.
      ${If} ${Silent}
      ${OrIf} $PassiveMode = 1
        SetErrorLevel 70
        Goto stock_desk_cleanup_done
      ${EndIf}

      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "$(stockDeskCleanupFailed)" IDRETRY stock_desk_cleanup_retry IDCANCEL stock_desk_cleanup_done

    stock_desk_cleanup_done:
  ${EndIf}
!macroend
