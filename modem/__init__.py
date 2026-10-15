"""
Phone-BBS Modem - Bell 103 Modem for VoIP G.711
================================================
A 300 baud FSK modem implementation for connecting to phone-only BBSes
via VoIP (G.711) services.
"""

from .bell103 import Bell103Modem, selftest
from .audio import AudioStream

__all__ = ['Bell103Modem', 'AudioStream', 'selftest']
