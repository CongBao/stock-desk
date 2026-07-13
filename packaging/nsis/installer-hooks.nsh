; Product-specific uninstall behavior belongs in supported Tauri NSIS hooks.

Var StockDeskCleanupReady
Var StockDeskCleanupExitCode

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
