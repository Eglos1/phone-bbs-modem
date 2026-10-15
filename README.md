# Phone-BBS Modem

Dial retro **BBSes from a phone line** by speaking real modem tones over VoIP.
A pure-Python **Bell 103** (300 baud, FSK) soft-modem, an audio layer that
bridges it to a SIP/VoIP call or to a physical **acoustic coupler**, and a
browser extension + bridge so a web calling service can carry the tones.

```
   BBS  <--PSTN/VoIP-->  SIP client  <--G.711-->  Bell 103 modem  <--ASCII-->  you
```

## Layout
| path | purpose |
|------|---------|
| `modem/bell103.py` | Bell 103 FSK modulator/demodulator |
| `modem/audio.py` | audio I/O (sounddevice / pyaudio) |
| `sip/` | SIP/VoIP client + provider presets |
| `bridge/` | local server + browser extension to route call audio |
| `terminal/` | interactive terminal UI |
| `pulse_bridge.py` | PipeWire/PulseAudio virtual-sink audio routing |
| `example.py` | runnable demos (see below) |
| `modem_sounds.py` | tone generator util |

## Install
```sh
pip install -r requirements.txt      # numpy, scipy, sounddevice, pyaudio, pyvoip
```

## Run
```sh
python example.py selftest        # modem loopback self-test (no hardware)
python example.py demo            # local ping-pong, no VoIP/audio
python example.py coupler         # acoustic coupler (hold phone to speaker)
python example.py dial            # interactive BBS dialer (needs SIP creds)
python example.py terminal        # interactive terminal with audio
python example.py list-providers  # show built-in VoIP providers
```

> Bring your own SIP/VoIP credentials; choose providers responsibly and follow
> each network's terms.

## License
MIT
