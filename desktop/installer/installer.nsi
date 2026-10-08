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
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  SetShellVarContext current
  IfFileExists "$INSTDIR\*.*" 0 fresh
  IfFileExists "$INSTDIR\.gui-agent-program" owned 0
  MessageBox MB_OK|MB_ICONSTOP "目标目录已有其他文件，请先卸载旧版 GUI Agent。"
  Abort
  owned:
    ExecWait '"$INSTDIR\runtime\python.exe" -B -m gua.app_cleanup' $0
    ${If} $0 != 0
      MessageBox MB_OK|MB_ICONSTOP "请先关闭 GUI Agent，然后重试安装。"
      Abort
    ${EndIf}
  fresh:
FunctionEnd

Section "GUI Agent"
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  File /r "${PAYLOAD}\*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "$DESKTOP\GUI Agent.lnk" "$INSTDIR\GUIAgent.exe"
  CreateShortcut "$SMPROGRAMS\GUI Agent.lnk" "$INSTDIR\GUIAgent.exe"
  WriteRegStr HKCU "Software\cszf1\GUI Agent" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "DisplayName" "GUI Agent"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "UninstallString" '$"$INSTDIR\Uninstall.exe$"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "QuietUninstallString" '$"$INSTDIR\Uninstall.exe$" /S'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  ExecWait '"$INSTDIR\runtime\python.exe" -B -m gua.app_cleanup --uninstall' $0
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "清理尚未完成。请关闭 GUI Agent 后再次卸载；程序与注册表项暂时保留。"
    Abort
  ${EndIf}
  SetOutPath "$TEMP"
  Delete "$DESKTOP\GUI Agent.lnk"
  Delete "$SMPROGRAMS\GUI Agent.lnk"
  DeleteRegKey HKCU "Software\cszf1\GUI Agent"
  DeleteRegKey /ifempty HKCU "Software\cszf1"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent"
  RMDir /r "$INSTDIR"
SectionEnd
