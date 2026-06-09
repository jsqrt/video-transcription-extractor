from app.security.network_isolation import enforce_offline_mode

enforce_offline_mode()

from app.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
