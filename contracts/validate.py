"""Validate the active v2 pack; --legacy checks archived v1 only."""
import sys

if __name__ == "__main__":
    if "--legacy" in sys.argv:
        from validate_v1 import main
    else:
        from validate_v2 import main
    main()
