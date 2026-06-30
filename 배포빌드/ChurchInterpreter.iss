; ChurchInterpreter Inno Setup Script
; 빌드: iscc ChurchInterpreter.iss

#define AppName "ChurchInterpreter"
#define AppVersion "1.0"
#define AppPublisher "Church"
#define AppExeName "ChurchInterpreter.exe"
#define SourceDir "dist\ChurchInterpreter"

[Setup]
AppId={{B7C2A1D4-3F8E-4A9B-B6C5-1D2E3F4A5B6C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=installer
OutputBaseFilename=ChurchInterpreter_Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
; 설치 후 자동 실행 제거 (사용자가 직접 실행)
DisableFinishedPage=no
; 관리자 권한 없어도 설치 가능하게
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile=

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 아이콘:"

[Files]
; 메인 실행 파일
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; 내부 라이브러리 폴더 (_internal)
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; ngrok (dist 폴더에 ngrok.exe가 있을 경우 포함)
Source: "ngrok.exe"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

; 사용설명서
Source: "manual.html"; DestDir: "{app}"; Flags: ignoreversion

; 기본 용어집 (없으면 서버가 내장 파일 사용)
Source: "glossary.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
; 바탕화면 바로가기
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon; Comment: "실시간 설교 동시통역"

; 시작 메뉴
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Comment: "실시간 설교 동시통역"
Name: "{group}\사용설명서"; Filename: "{app}\manual.html"
Name: "{group}\설정 화면 열기"; Filename: "http://localhost:5000/setup"
Name: "{group}\{#AppName} 제거"; Filename: "{uninstallexe}"

[Run]
; 설치 완료 후 사용설명서 열기 (선택)
Filename: "{app}\manual.html"; Description: "사용설명서 보기"; Flags: postinstall shellexec skipifsilent unchecked

; 설치 완료 후 프로그램 실행 (선택)
Filename: "{app}\{#AppExeName}"; Description: "ChurchInterpreter 바로 실행"; Flags: postinstall nowait skipifsilent

[Code]
// 설치 완료 후 PATH에 app 폴더 추가 (ngrok 실행을 위해)
procedure CurStepChanged(CurStep: TSetupStep);
var
  OldPath, NewPath: string;
begin
  if CurStep = ssPostInstall then begin
    if RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OldPath) then begin
      if Pos(ExpandConstant('{app}'), OldPath) = 0 then begin
        NewPath := OldPath + ';' + ExpandConstant('{app}');
        RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', NewPath);
      end;
    end;
  end;
end;
