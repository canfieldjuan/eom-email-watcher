Var EW_SOURCE_CUTOVER_MUTEX
!define EW_HOOK_DIR "${__FILEDIR__}"
!macro EW_SOURCE_CUTOVER PHASE
  Push $0
  Push $1
  Push $8
  Push $9
  !if "${PHASE}" == "Pre"
    System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Local\EomEmailWatcherSourceCutoverV1") p.r0'
    StrCpy $EW_SOURCE_CUTOVER_MUTEX $0
    System::Call 'kernel32::WaitForSingleObject(p r0, i 0) i.r1'
    ${If} $1 != 0
    ${AndIf} $1 != 128
      MessageBox MB_OK|MB_ICONSTOP "Another Email Watcher update is already running."
      Abort
    ${EndIf}
  !endif
  StrCpy $8 "$OUTDIR"
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=ew-source-cutover.ps1 "${EW_HOOK_DIR}\cutover.ps1"
  System::Call 'kernel32::GetCurrentProcessId() i.r9'
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "$PLUGINSDIR\ew-source-cutover.ps1" -Phase ${PHASE} -InstallRoot "$INSTDIR" -InstallerPid $9'
  Pop $0
  Pop $1
  SetOutPath "$8"
  ${If} $0 != 0
    DetailPrint "Email Watcher source-lock update could not qualify: $1"
    MessageBox MB_OK|MB_ICONSTOP "Email Watcher could not stop and qualify its previous installation. The update was stopped."
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
