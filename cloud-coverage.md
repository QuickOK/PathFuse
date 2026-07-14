# Ku/Ka-band satellite links and thunderstorm cloud coverage

## Summary

Smoke does not affect the satellite link because particulate matter is orders of magnitude
smaller than Ku/Ka wavelengths and does not couple to the signal. Liquid water is
the opposite: it attenuates strongly at those frequencies. A thunderstorm cell can
degrade service **even when no rain is reaching the dish**, through several distinct
mechanisms.

## Mechanisms

### Cloud liquid water content
Cumulonimbus has by far the highest liquid water density of any cloud type — on the
order of 1–3+ g/m³ versus ~0.05–0.25 g/m³ for stratus. Cloud attenuation (modeled by
ITU-R P.840) scales roughly with frequency squared across the Ku/Ka range, so a thick
convective tower in the slant path produces measurable attenuation with zero rain at
ground level. Smaller than rain fade, but not negligible when the cell is dense.

### Rain aloft / virga
"Not raining at the ground" does not mean no hydrometeors in the path. A cell can be
dumping heavy precipitation in the column that evaporates before reaching the deck
(virga). That water sits in the line of sight to the satellite and attenuates like
rain fade — because it physically *is* rain, just not at the dish's elevation.

### Melting layer / mixed phase
Wet, melting hydrometeors near the 0 °C level scatter more than either dry ice or
liquid rain alone. A vigorous updraft loading the column with graupel and supercooled
water in the slant path contributes here as well.

### Gateway-side weather
Even with a clear sky over the dish, a cell parked over the serving ground station
degrades the feeder link. This shows up as throughput/latency hits with no local
weather to explain it.

## Observable behavior

In the terminal's own stats this typically appears as SNR drops and obstruction-like outage
seconds rather than a hard disconnect, since the constellation reroutes around cells
when it can.

## Practical impact (mobile stack)

The Engarde master/backup logic may flap the satellite WAN even on a "dry" day if a strong cell
crosses the slant path. This is exactly the case where keeping cellular warm as backup
earns its keep.

## Reference models
- **ITU-R P.840** — attenuation due to clouds and fog (cloud liquid water).
- **ITU-R P.618** — rain attenuation prediction for slant paths.
