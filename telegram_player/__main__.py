try:
    from .app import main
except ImportError:
    # Frozen onedir: __main__.py is executed as a top-level script, so relative
    # imports have no package context. Import the module directly instead.
    from telegram_player.app import main

if __name__ == "__main__":
    main()