"""
Build script for the Modern LoL Win Probability Overlay.

Creates a single executable using PySide6 (Qt) that runs without a console window.
Optimized build - only includes required Qt modules.
"""

import subprocess
import sys
import shutil
from pathlib import Path


def build():
    """Build the native overlay executable."""
    
    # Paths
    script_dir = Path(__file__).parent
    project_root = script_dir.parent.parent
    overlay_script = script_dir / "overlay.pyw"
    models_dir = project_root / "models"
    dist_dir = script_dir.parent / "native-dist"
    
    # Required model files
    model_files = [
        models_dir / "lol_win_predictor_lgbm_isotonic.joblib",
        models_dir / "features.joblib",
    ]
    
    # Verify model files exist
    missing = [f for f in model_files if not f.exists()]
    if missing:
        print("ERROR: Missing model files:")
        for f in missing:
            print(f"  - {f}")
        print("\nRun the training notebook (analysis.ipynb) first to generate these files.")
        sys.exit(1)
    
    # Clean previous build
    build_dir = script_dir / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    
    # Try to clean dist dir, but continue if it fails (file locked)
    if dist_dir.exists():
        try:
            shutil.rmtree(dist_dir)
        except PermissionError:
            print("  Warning: Could not clean dist dir (file may be in use)")
            print("  Building anyway...")
    
    dist_dir.mkdir(exist_ok=True)
    
    # Build command - optimized for minimal size
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--name", "LoL-Win-Probability-Overlay-v4",
        "--distpath", str(dist_dir),
        "--workpath", str(build_dir),
        "--specpath", str(script_dir),
        # Add model files
        "--add-data", f"{models_dir / 'lol_win_predictor_lgbm_isotonic.joblib'};models",
        "--add-data", f"{models_dir / 'features.joblib'};models",
        # Only the Qt modules we actually use (Core, Gui, Widgets)
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtWidgets",
        # sklearn essentials
        "--hidden-import", "sklearn.ensemble._forest",
        "--hidden-import", "sklearn.tree._classes",
        "--hidden-import", "sklearn.neighbors._partition_nodes",
        "--hidden-import", "sklearn.calibration",
        "--hidden-import", "sklearn.isotonic",
        # LightGBM
        "--hidden-import", "lightgbm",
        # Exclude unnecessary Qt modules to reduce size
        "--exclude-module", "PySide6.Qt3DAnimation",
        "--exclude-module", "PySide6.Qt3DCore",
        "--exclude-module", "PySide6.Qt3DExtras",
        "--exclude-module", "PySide6.Qt3DInput",
        "--exclude-module", "PySide6.Qt3DLogic",
        "--exclude-module", "PySide6.Qt3DRender",
        "--exclude-module", "PySide6.QtBluetooth",
        "--exclude-module", "PySide6.QtCharts",
        "--exclude-module", "PySide6.QtDataVisualization",
        "--exclude-module", "PySide6.QtDesigner",
        "--exclude-module", "PySide6.QtGraphs",
        "--exclude-module", "PySide6.QtGraphsWidgets",
        "--exclude-module", "PySide6.QtHelp",
        "--exclude-module", "PySide6.QtHttpServer",
        "--exclude-module", "PySide6.QtLocation",
        "--exclude-module", "PySide6.QtMultimedia",
        "--exclude-module", "PySide6.QtMultimediaWidgets",
        "--exclude-module", "PySide6.QtNetworkAuth",
        "--exclude-module", "PySide6.QtNfc",
        "--exclude-module", "PySide6.QtOpenGL",
        "--exclude-module", "PySide6.QtOpenGLWidgets",
        "--exclude-module", "PySide6.QtPdf",
        "--exclude-module", "PySide6.QtPdfWidgets",
        "--exclude-module", "PySide6.QtPositioning",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.QtQuick3D",
        "--exclude-module", "PySide6.QtQuickControls2",
        "--exclude-module", "PySide6.QtQuickWidgets",
        "--exclude-module", "PySide6.QtRemoteObjects",
        "--exclude-module", "PySide6.QtScxml",
        "--exclude-module", "PySide6.QtSensors",
        "--exclude-module", "PySide6.QtSerialBus",
        "--exclude-module", "PySide6.QtSerialPort",
        "--exclude-module", "PySide6.QtSpatialAudio",
        "--exclude-module", "PySide6.QtSql",
        "--exclude-module", "PySide6.QtStateMachine",
        "--exclude-module", "PySide6.QtSvg",
        "--exclude-module", "PySide6.QtSvgWidgets",
        "--exclude-module", "PySide6.QtTest",
        "--exclude-module", "PySide6.QtTextToSpeech",
        "--exclude-module", "PySide6.QtWebChannel",
        "--exclude-module", "PySide6.QtWebEngine",
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtWebSockets",
        "--exclude-module", "PySide6.QtXml",
        # Exclude other heavy modules we don't need
        "--exclude-module", "matplotlib",
        "--exclude-module", "PIL",
        "--exclude-module", "tkinter",
        "--exclude-module", "IPython",
        "--exclude-module", "jupyter",
        "--exclude-module", "notebook",
        "--exclude-module", "pytest",
        str(overlay_script),
    ]
    
    print("=" * 60)
    print("  Building Modern LoL Win Probability Overlay")
    print("  Using PySide6 (Qt) - Optimized build")
    print("=" * 60)
    print(f"\n  Source: {overlay_script}")
    print(f"  Output: {dist_dir}")
    print()
    
    # Run PyInstaller
    result = subprocess.run(cmd, cwd=str(script_dir))
    
    if result.returncode == 0:
        exe_path = dist_dir / "LoL-Win-Probability-Overlay-v4.exe"
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / 1024 / 1024
            print()
            print("=" * 60)
            print("  BUILD SUCCESSFUL!")
            print("=" * 60)
            print(f"\n  Executable: {exe_path}")
            print(f"  Size: {size_mb:.1f} MB")
            print()
            print("  Run the executable to start the overlay.")
            print("  No console window will appear - just the overlay!")
            print()
        else:
            print("\nBUILD FAILED: Executable not found")
            sys.exit(1)
    else:
        print()
        print("BUILD FAILED!")
        sys.exit(1)


if __name__ == "__main__":
    build()
