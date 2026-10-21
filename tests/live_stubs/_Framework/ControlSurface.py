class ControlSurface(object):
    """STUB of _Framework.ControlSurface for headless tests."""
    def __init__(self, c_instance):
        self._c_instance = c_instance
        self.messages = []
        self.logs = []

    def song(self):
        return self._c_instance.song()

    def show_message(self, msg):
        self.messages.append(msg)

    def log_message(self, msg):
        self.logs.append(msg)

    def disconnect(self):
        self.disconnected = True
