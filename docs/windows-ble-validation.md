# Windows BLE hardware validation

Run this checklist on physical Windows 10 and Windows 11 laptops; CI cannot validate radios, drivers, trainer firmware, or signing.

- Install with `pip install ".[ble]"`; confirm the Ride page reports BLE available.
- Pair/test an FTMS smart trainer and verify ERG target changes, control acquisition, and clean disconnect/reconnect.
- Pair/test a Cycling Power Service power meter; verify watts and cadence update and are saved.
- Pair/test a Heart Rate Service sensor; verify live HR and saved HR streams.
- Test trainer + separate CPS + HR simultaneously and confirm the chosen sources remain stable.
- Stop pedaling: confirm automatic pause; resume: confirm elapsed workout time and segment progression remain correct.
- Complete and manually stop rides; confirm duration, power, cadence, HR, and distance streams persist.
- Open each saved activity; confirm power-zone and heart-rate-zone time totals and coverage match the streams.
- Disable Bluetooth and remove a sensor mid-ride; confirm actionable errors, no app crash, and safe session save/stop.
- Repeat after sleep/wake and with Bluetooth privacy permission denied then restored.

Record Windows build, adapter, device models, firmware, Bleak version, logs, and pass/fail for each item. Code signing must be validated separately with the release certificate; distributed CI artifacts are explicitly unsigned.
