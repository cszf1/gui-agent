Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
Name "GUI Agent"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\GUI Agent"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32
Icon "${ICON}"
UninstallIcon "${ICON}"
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  SetShellVarContext current
  SetRegView 64
  IfFileExists "$INSTDIR\*.*" 0 fresh
  IfFileExists "$INSTDIR\.gui-agent-program" owned 0
  MessageBox MB_OK|MB_ICONSTOP "目标目录已有其他文件，请先卸载旧版 GUI Agent。" /SD IDOK
  SetErrorLevel 2
  Abort
  owned:
    ExecWait '"$INSTDIR\runtime\python.exe" -B -m gua.app_cleanup' $0
    ${If} $0 != 0
      MessageBox MB_OK|MB_ICONSTOP "请先关闭 GUI Agent，然后重试安装。" /SD IDOK
      SetErrorLevel 2
      Abort
    ${EndIf}
    ; Replace runtime/UI directories as a unit so removed dependencies and old
    ; hashed assets do not accumulate across successful upgrades. User data is
    ; outside these directories.
    SetOutPath "$TEMP"
    RMDir /r "$INSTDIR\runtime"
    RMDir /r "$INSTDIR\ui"
  fresh:
FunctionEnd

Section "GUI Agent"
  SectionIn RO
  SetShellVarContext current
  SetRegView 64
  SetOutPath "$INSTDIR"
  File /r "${PAYLOAD}\*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "$SMPROGRAMS\GUI Agent.lnk" "$INSTDIR\GUIAgent.exe"
  ; Remove the redundant application key left by v0.8 upgrades.
  DeleteRegKey HKCU "Software\cszf1\GUI Agent"
  DeleteRegKey /ifempty HKCU "Software\cszf1"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "DisplayName" "GUI Agent"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "NoRepair" 1
SectionEnd

Section /o "桌面快捷方式"
  SetShellVarContext current
  CreateShortcut "$DESKTOP\GUI Agent.lnk" "$INSTDIR\GUIAgent.exe"
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  SetRegView 64
  ExecWait '"$INSTDIR\runtime\python.exe" -B -m gua.app_cleanup --uninstall' $0
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "清理尚未完成。请关闭 GUI Agent 后再次卸载；程序与注册表项暂时保留。" /SD IDOK
    SetErrorLevel 2
    Abort
  ${EndIf}
  SetOutPath "$TEMP"
  Delete "$DESKTOP\GUI Agent.lnk"
  Delete "$SMPROGRAMS\GUI Agent.lnk"
  DeleteRegKey HKCU "Software\cszf1\GUI Agent"
  DeleteRegKey /ifempty HKCU "Software\cszf1"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent"
  ; app_cleanup has stopped the backend and removed junctions before any
  ; Delete operation. Preserve user-added files by removing only our manifest.
  !include "${MANIFEST_DIR}\files_uninstall.nsh"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
