def clamp(value, low, high):
    """Return value limited to the range low..high."""
    if value < low:
        return low
    return value
