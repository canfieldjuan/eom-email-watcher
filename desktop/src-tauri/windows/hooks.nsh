Var EW_SOURCE_CUTOVER_MUTEX
Var EW_SOURCE_CUTOVER_PS
Var EW_SOURCE_CUTOVER_PID
Var EW_SOURCE_CUTOVER_LOG
!define EW_HOOK_DIR "${__FILEDIR__}"
!macro EW_SOURCE_CUTOVER_RECORD TEXT
  ; Preserve the actual invocation result even when silent NSIS has no stdout.
  DetailPrint "Email Watcher cutover ${TEXT}"
  ClearErrors
  FileOpen $EW_SOURCE_CUTOVER_LOG "$TEMP\eom-email-watcher-cutover-$EW_SOURCE_CUTOVER_PID.log" a
  IfErrors +3 0
    FileWrite $EW_SOURCE_CUTOVER_LOG "${TEXT}$\r$\n"
    FileClose $EW_SOURCE_CUTOVER_LOG
  ClearErrors
!macroend
!macro EW_SOURCE_CUTOVER PHASE
  Push $0
  Push $1
  Push $8
  Push $9
  System::Call 'kernel32::GetCurrentProcessId() i.r9'
  StrCpy $EW_SOURCE_CUTOVER_PID $9
  StrCpy $8 "$OUTDIR"
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=ew-source-cutover.ps1 "${EW_HOOK_DIR}\cutover.ps1"
  ; NSIS is x86: Sysnative crosses WOW64 redirection to native PowerShell.
  StrCpy $EW_SOURCE_CUTOVER_PS "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe"
  IfFileExists "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" 0 +2
    StrCpy $EW_SOURCE_CUTOVER_PS "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
  !if "${PHASE}" == "Pre"
    !insertmacro EW_PREPARE_PACKAGE_CATALOG
    ; One name owner in cutover.ps1, shared with the restoration worker.
    nsExec::ExecToStack /TIMEOUT=10000 '"$EW_SOURCE_CUTOVER_PS" -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "$PLUGINSDIR\ew-source-cutover.ps1" -Phase Name -InstallRoot "$INSTDIR"'
    Pop $0
    Pop $1
    !insertmacro EW_SOURCE_CUTOVER_RECORD "phase=Name exit=$0 output=$1"
    ${If} $0 != 0
      SetOutPath "$8"
      Abort
    ${EndIf}
    System::Call 'kernel32::CreateMutexW(p 0, i 0, w r1) p.r0'
    StrCpy $EW_SOURCE_CUTOVER_MUTEX $0
    System::Call 'kernel32::WaitForSingleObject(p r0, i 0) i.r1'
    !insertmacro EW_SOURCE_CUTOVER_RECORD "phase=Mutex handle=$0 wait=$1"
    ${If} $1 != 0
    ${AndIf} $1 != 128
      SetOutPath "$8"
      MessageBox MB_OK|MB_ICONSTOP "Another Email Watcher update is already running." /SD IDOK
      Abort
    ${EndIf}
  !endif
  System::Call 'kernel32::GetCurrentProcessId() i.r9'
  nsExec::ExecToStack /TIMEOUT=120000 '"$EW_SOURCE_CUTOVER_PS" -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "$PLUGINSDIR\ew-source-cutover.ps1" -Phase ${PHASE} -InstallRoot "$INSTDIR" -InstallerPid $9 -ProgramCatalog "$EW_PACKAGE_PROGRAM_CATALOG"'
  Pop $0
  Pop $1
  !insertmacro EW_SOURCE_CUTOVER_RECORD "phase=${PHASE} exit=$0 output=$1"
  SetOutPath "$8"
  ${If} $0 != 0
    DetailPrint "Email Watcher source-lock update could not qualify: $1"
    MessageBox MB_OK|MB_ICONSTOP "Email Watcher could not qualify this update. Close its running processes and retry the installer." /SD IDOK
    Abort
  ${EndIf}
  !if "${PHASE}" == "Post"
    System::Call 'kernel32::ReleaseMutex(p $EW_SOURCE_CUTOVER_MUTEX)'
    System::Call 'kernel32::CloseHandle(p $EW_SOURCE_CUTOVER_MUTEX)'
  !endif
  Pop $9
  Pop $8
  Pop $1
  Pop $0
!macroend
!macro NSIS_HOOK_PREINSTALL
  !insertmacro EW_SOURCE_CUTOVER Pre
!macroend
!macro NSIS_HOOK_POSTINSTALL
  !insertmacro EW_SOURCE_CUTOVER Post
!macroend
