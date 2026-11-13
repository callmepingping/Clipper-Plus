"""PyInstaller entry point. Kept separate from the engine process."""
import multiprocessing
from videomind.native_host import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
