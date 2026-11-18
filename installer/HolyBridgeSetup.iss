; Instalador de HolyBridge 6.5.3 (Inno Setup 7)
; Compilar:  "C:\Program Files\Inno Setup 7\ISCC.exe" installer\HolyBridgeSetup.iss
; Toma la carpeta portable ya armada (public/, lib/, ndi/, runtime/) y crea
; accesos, entrada en "Aplicaciones instaladas" y desinstalador.

#define AppName "HolyBridge"
#define AppVersion "6.5.3"
#define AppExe "HolyBridgeProv6.5.3.exe"
#define AppUrl "http://127.0.0.1:7333/"

[Setup]
AppId={{8F1C2B54-6D7A-4A0E-9C31-7B5C0E2A9D14}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=HolyBridge Pro
AppPublisherURL=https://github.com/becerrapedro4/holybridgepro-kinetic
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
; Sin UAC: se instala en %LOCALAPPDATA%\Programs\HolyBridge y funciona igual.
; Sin diálogo de "todos los usuarios / solo yo": con =dialog el instalador
; silencioso se quedaba esperando esa ventana.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
OutputDir=..\dist
OutputBaseFilename=HolyBridge-6.5.3-Setup
SetupIconFile=..\portable\HolyBridge-6.5.3-portable\HolyBridge.ico
UninstallDisplayIcon={app}\HolyBridge.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
DisableWelcomePage=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
AllowNoIcons=yes
CloseApplications=no

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"; Flags: checkedonce

[Files]
Source: "..\portable\HolyBridge-6.5.3-portable\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "kinetic_ndi.log,*.zip,*.log"

[Icons]
; El .vbs arranca el bridge oculto (Node incluido) y después la interfaz.
Name: "{group}\{#AppName}"; Filename: "{app}\Iniciar HolyBridge.vbs"; IconFilename: "{app}\HolyBridge.ico"; WorkingDir: "{app}"; Comment: "Abrir {#AppName}"
Name: "{group}\Overlay para OBS / vMix"; Filename: "http://127.0.0.1:7333/"; IconFilename: "{app}\HolyBridge.ico"; Comment: "URL de la fuente navegador (el panel se abre dentro de HolyBridge)"
Name: "{group}\Carpeta de instalación"; Filename: "{app}"
Name: "{group}\Desinstalar {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Iniciar HolyBridge.vbs"; IconFilename: "{app}\HolyBridge.ico"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Iniciar HolyBridge.vbs"; Description: "Abrir {#AppName} ahora"; Flags: postinstall nowait skipifsilent shellexec

[UninstallDelete]
Type: filesandordirs; Name: "{app}\public\fonts"
Type: files; Name: "{app}\kinetic_ndi.log"
