# PyInstaller spec — standalone arm64 .app, no system Python dependency.
# Qt modules beyond Core/Gui/Widgets are excluded: this is a plain widgets app,
# and leaving QML/Quick/WebEngine in triples the bundle size.

EXCLUDED_QT = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.QtNetwork", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.Qt3DCore", "PySide6.QtDesigner",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPrintSupport",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSerialPort", "PySide6.QtSensors",
    "PySide6.QtTextToSpeech", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSpatialAudio", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtConcurrent", "PySide6.QtXml",
]

EXCLUDED_STDLIB = [
    "tkinter", "test", "unittest", "pydoc_data", "sqlite3", "xmlrpc",
    "html", "http", "urllib", "distutils", "setuptools", "pip",
    "numpy", "PIL", "pytest",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=EXCLUDED_QT + EXCLUDED_STDLIB,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Claude Migrator",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Claude Migrator",
)

app = BUNDLE(
    coll,
    name="Claude Migrator.app",
    icon="AppIcon.icns",
    bundle_identifier="com.github.pokharnajay.claudemigrator",
    version="1.0",
    info_plist={
        "CFBundleName": "Claude Migrator",
        "CFBundleDisplayName": "Claude Migrator",
        "CFBundleShortVersionString": "1.0",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSRequiresAquaSystemAppearance": False,
    },
)
