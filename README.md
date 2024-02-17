# dockable_probe_voron
This a modified version of [cloakedcode dockable_probe](https://github.com/cloakedcode/klipper/blob/work-annex-probe/docs/Dockable_Probe.md)
The dockable_probe.cfg is the actual config I use on my Voron BUT I have Origin to bed center so the coords may seem off for normal vorons so it is a example only.

The reason I modified cloakedcodes dockable probe was the carriage was returning to the last probe position after a dock with the quad gantry level it returned to the opposite end of the bed from where the dock was totally unnecessary for me, same when using prolofts auto z calibration which needs a patch from his website ie his [dev version](https://github.com/protoloft/klipper_z_calibration/commit/ad8ef52b5afbc69193885895fe8737f46e36fb28)  I have also a updated [z-calibration](https://github.com/BlackStump/klipper_z_calibration)

The code that was returning to last probe position is still intact but now is a option to turn off in the config, for my use case I have it off maybe as it was ie on is better suited for other uses.

This is the section in the dockable_probe.cfg
````pytho
return_to_last_probe_position_after_detach: False
````
I also included attach and detach speed in the config. 
