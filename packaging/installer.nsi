; NSIS installer for tzmrit-display.
; Per-user install (no admin), uninstaller registered in Apps & Features.
; Build with packaging\build.bat - expects PyInstaller output in
; packaging\dist\tzmrit-display\.

!include "MUI2.nsh"
!include "Sections.nsh"
!include "FileFunc.nsh"

!define APPNAME "tzmrit-display"
; What the user sees (installer window, Start menu, Apps & Features). The
; technical name above stays in the install dir, registry key, exe and
; setup-file names; upgrades must clean up shortcuts under the OLD name too.
!define APPNAME_DISPLAY "TZMRIT Display"
!ifndef VERSION
  !error "VERSION is not defined. Build with packaging\build.bat - it passes /DVERSION from pyproject.toml, the single source of the version."
!endif
!ifndef VERSION_NUMERIC
  !error "VERSION_NUMERIC is not defined. Build with packaging\build.bat - it derives the strict X.X.X.X form that VIProductVersion requires from the same package version as VERSION."
!endif
!define PUBLISHER "Torsten Raudssus"
!define URL "https://github.com/Getty/tzmrit-display"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"

Name "${APPNAME_DISPLAY} ${VERSION}"
OutFile "tzmrit-display-setup-${VERSION}.exe"
Unicode True
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\${APPNAME}"
InstallDirRegKey HKCU "${UNINST_KEY}" "InstallLocation"

Var AutostartSel
Var AutostartArgs

; the setup exe and the uninstaller carry the app icon; the shortcuts get
; theirs from the target exes (embedded via tzmrit-display.spec)
!define MUI_ICON "icon.ico"
!define MUI_UNICON "icon.ico"

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

; version resource of the setup exe itself (Properties -> Details);
; VIProductVersion only accepts strict X.X.X.X, hence VERSION_NUMERIC
VIProductVersion "${VERSION_NUMERIC}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "ProductName" "${APPNAME_DISPLAY}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "CompanyName" "${PUBLISHER}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "FileDescription" "${APPNAME_DISPLAY} installer"
VIAddVersionKey /LANG=${LANG_ENGLISH} "LegalCopyright" "Copyright (c) 2026 ${PUBLISHER}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "ProductVersion" "${VERSION}"

; -- install -----------------------------------------------------------------

Section "${APPNAME_DISPLAY} (required)" SecCore
  SectionIn RO
  ; stop a running instance so files can be replaced
  nsExec::Exec 'taskkill /f /im tzmrit-displayw.exe'
  nsExec::Exec 'taskkill /f /im tzmrit-display.exe'
  Sleep 500

  SetOutPath "$INSTDIR"
  File /r "dist\tzmrit-display\*.*"

  CreateDirectory "$SMPROGRAMS\${APPNAME_DISPLAY}"
  CreateShortcut "$SMPROGRAMS\${APPNAME_DISPLAY}\${APPNAME_DISPLAY}.lnk" \
                 "$INSTDIR\tzmrit-displayw.exe" "run"
  CreateShortcut "$SMPROGRAMS\${APPNAME_DISPLAY}\${APPNAME_DISPLAY} (Claude sessions).lnk" \
                 "$INSTDIR\tzmrit-displayw.exe" "run --claude"
  ; windowed variant so stopping never flashes a console
  CreateShortcut "$SMPROGRAMS\${APPNAME_DISPLAY}\Stop ${APPNAME_DISPLAY}.lnk" \
                 "$INSTDIR\tzmrit-displayw.exe" "stop"
  CreateShortcut "$SMPROGRAMS\${APPNAME_DISPLAY}\Uninstall.lnk" "$INSTDIR\uninstall.exe"

  ; the autostart sections below recreate this if selected
  Delete "$SMSTARTUP\${APPNAME_DISPLAY}.lnk"

  ; upgrade migration: versions before the "${APPNAME_DISPLAY}" naming put
  ; both the Start menu folder and the startup shortcut under the technical
  ; name - remove them so an upgrade leaves no dead duplicates
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  Delete "$SMSTARTUP\${APPNAME}.lnk"

  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APPNAME_DISPLAY}"
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

; optional autostart: at most one of the two sections below may be selected
; (enforced in .onSelChange); selecting neither means no autostart
Section /o "Start at logon: system dashboard" SecAutoPlain
  StrCpy $AutostartArgs "run"
  CreateShortcut "$SMSTARTUP\${APPNAME_DISPLAY}.lnk" "$INSTDIR\tzmrit-displayw.exe" "run"
SectionEnd

Section "Start at logon: dashboard with Claude sessions" SecAutoClaude
  StrCpy $AutostartArgs "run --claude"
  CreateShortcut "$SMSTARTUP\${APPNAME_DISPLAY}.lnk" "$INSTDIR\tzmrit-displayw.exe" "run --claude"
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCore} \
    "Installs the ${APPNAME_DISPLAY} program files and Start Menu shortcuts. Required."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAutoPlain} \
    "Creates a shortcut in the Windows Startup folder so the system dashboard starts automatically when you log on. At most one autostart option can be selected; select neither for no autostart."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAutoClaude} \
    "Creates a shortcut in the Windows Startup folder so the dashboard with Claude sessions starts automatically when you log on. At most one autostart option can be selected; select neither for no autostart."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Function .onInit
  StrCpy $AutostartSel ${SecAutoClaude}
  StrCpy $AutostartArgs "run"
FunctionEnd

; one-or-none behaviour for the two autostart sections: checking one unchecks
; the other, unchecking both stays allowed
Function .onSelChange
  ${If} ${SectionIsSelected} ${SecAutoPlain}
  ${AndIf} ${SectionIsSelected} ${SecAutoClaude}
    ${If} $AutostartSel == ${SecAutoPlain}
      !insertmacro UnselectSection ${SecAutoPlain}
      StrCpy $AutostartSel ${SecAutoClaude}
    ${Else}
      !insertmacro UnselectSection ${SecAutoClaude}
      StrCpy $AutostartSel ${SecAutoPlain}
    ${EndIf}
  ${ElseIf} ${SectionIsSelected} ${SecAutoPlain}
    StrCpy $AutostartSel ${SecAutoPlain}
  ${ElseIf} ${SectionIsSelected} ${SecAutoClaude}
    StrCpy $AutostartSel ${SecAutoClaude}
  ${Else}
    StrCpy $AutostartSel -1
  ${EndIf}
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

  ; both namings, defensively: the display name (current installer and dev
  ; install.bat) and the technical name (anything older)
  Delete "$SMSTARTUP\${APPNAME_DISPLAY}.lnk"
  Delete "$SMSTARTUP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME_DISPLAY}"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
SectionEnd
