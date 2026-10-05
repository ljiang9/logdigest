"""Enables `python -m logdigest`."""
if __package__:
    from .logdigest import main
else:
    from logdigest import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
