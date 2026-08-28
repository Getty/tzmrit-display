; NSIS installer for tzmrit-display.
; Per-user install (no admin), uninstaller registered in Apps & Features.
; Build with packaging\build.bat - expects PyInstaller output in
; packaging\dist\tzmrit-display\.

!include "MUI2.nsh"
!include "Sections.nsh"
!include "FileFunc.nsh"

!define APPNAME "tzmrit-display"
!ifndef VERSION
  !error "VERSION is not defined. Build with packaging\build.bat - it passes /DVERSION from pyproject.toml, the single source of the version."
!endif
!define PUBLISHER "Torsten Raudssus"
!define URL "https://github.com/Getty/tzmrit-display"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"

Name "${APPNAME} ${VERSION}"
OutFile "tzmrit-display-setup-${VERSION}.exe"
Unicode True
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\${APPNAME}"
InstallDirRegKey HKCU "${UNINST_KEY}" "InstallLocation"

Var RadioState
Var AutostartArgs

!define MUI_COMPONENTSPAGE_NODESC
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Start the dashboard now"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchDashboard
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; -- install -----------------------------------------------------------------

Section "${APPNAME} (required)" SecCore
  SectionIn RO
  ; stop a running instance so files can be replaced
  nsExec::Exec 'taskkill /f /im tzmrit-displayw.exe'
  nsExec::Exec 'taskkill /f /im tzmrit-display.exe'
  Sleep 500

  SetOutPath "$INSTDIR"
  File /r "dist\tzmrit-display\*.*"

  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Dashboard.lnk" \
                 "$INSTDIR\tzmrit-displayw.exe" "run"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Dashboard with Claude sessions.lnk" \
                 "$INSTDIR\tzmrit-displayw.exe" "run --claude"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Uninstall.lnk" "$INSTDIR\uninstall.exe"

  ; the autostart sections below recreate this if selected
  Delete "$SMSTARTUP\${APPNAME}.lnk"

  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "URLInfoAbout" "${URL}"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\tzmrit-display.exe"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"
SectionEnd

SectionGroup /e "Start at logon" SecGroupAuto
  Section /o "System dashboard" SecAutoPlain
    StrCpy $AutostartArgs "run"
    CreateShortcut "$SMSTARTUP\${APPNAME}.lnk" "$INSTDIR\tzmrit-displayw.exe" "run"
  SectionEnd
  Section "Dashboard with Claude sessions" SecAutoClaude
    StrCpy $AutostartArgs "run --claude"
    CreateShortcut "$SMSTARTUP\${APPNAME}.lnk" "$INSTDIR\tzmrit-displayw.exe" "run --claude"
  SectionEnd
  Section /o "No autostart" SecAutoNone
  SectionEnd
SectionGroupEnd

; one-of-many behaviour for the autostart group
Function .onInit
  StrCpy $RadioState ${SecAutoClaude}
  StrCpy $AutostartArgs "run"
FunctionEnd

Function .onSelChange
  !insertmacro StartRadioButtons $RadioState
    !insertmacro RadioButton ${SecAutoPlain}
    !insertmacro RadioButton ${SecAutoClaude}
    !insertmacro RadioButton ${SecAutoNone}
  !insertmacro EndRadioButtons
FunctionEnd

Function LaunchDashboard
  Exec '"$INSTDIR\tzmrit-displayw.exe" $AutostartArgs'
FunctionEnd

; -- uninstall ---------------------------------------------------------------

Section "Uninstall"
  nsExec::Exec 'taskkill /f /im tzmrit-displayw.exe'
  nsExec::Exec 'taskkill /f /im tzmrit-display.exe'
  Sleep 500
  ; blank the panel so no stale image stays behind (best effort)
  nsExec::Exec '"$INSTDIR\tzmrit-display.exe" clear'

  Delete "$SMSTARTUP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
SectionEnd
