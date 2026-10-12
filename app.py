"""Entry point PyInstaller freezes into the Mac and Windows apps."""
import multiprocessing

if __name__ == "__main__":
    # Must come first: in the frozen app, each worker process starts this same program,
    # and freeze_support() turns it into a worker instead of opening another window.
    multiprocessing.freeze_support()
    from midi_tempo.gui import main

    main()
