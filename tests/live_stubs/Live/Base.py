TIMERS = []


class Timer(object):
    """Stub of Live.Base.Timer: tests fire ticks by calling fire_all()."""
    def __init__(self, callback, interval, repeat=False, start=False):
        self.callback, self.interval, self.repeat = callback, interval, repeat
        self.running = start
        TIMERS.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def restart(self):
        self.running = True


def fire_all():
    for t in list(TIMERS):
        if t.running:
            t.callback()
