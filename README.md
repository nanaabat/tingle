# tingle

hunch, tinker and wink as one experience inside the 3D tools you already use.

in one blender panel: hunch reads how a space tends to feel, wink picks changes toward the feel you want, and tinker builds them as a full editable copy beside your original. your original never changes. each round is a labelled version you can export, with before and after renders from matching cameras, and weather scenes such as golden hour, rain, harmattan haze and night.

hunch predicts how a space tends to feel from its geometry, materials, light and climate. it does not read people, and it is not a daylight, energy or weather compliance model.

## what is here

blender/tingle  the tingle panel, the hunch section, the feel loop, scene looks and weather scenes, and opt in run sharing
rhino/TingleCommands.cs  the tingle read command for the rhino plug in in the tinker repo
build_blender.py  builds the tingle download from the tinker and wink repos plus this one

## what is not here

the hunch engine. the panel calls a hunch service on your machine; without one, tinker and wink still work and the hunch section tells you it is missing. no trained weights, no run data, no models or renders of client buildings.

## build

```
git clone tinker wink and tingle side by side
python build_blender.py --tinker ../tinker --wink ../wink
```

then install the tinker zip from dist in blender (edit, preferences, get extensions, install from disk). set the hunch root and service python in the tingle panel, or HUNCH_ROOT and HUNCH_PYTHON.

## sharing

off by default. if you turn on share this run and set a share URL (in the panel, or TINGLE_SHARE_URL), one anonymised summary (scores, feature numbers, the changes kept) is posted to that URL. if no share URL is set, nothing is sent. no names, emails, paths or images. every run is also logged on your machine.

## status

experimental and early. expect rough edges and changes, and check every result yourself. no trained models ship with this repo. our internal tingle model is trained on synthetic bootstrap data and is not included. it runs end to end in blender 5.3 when a hunch service is installed. it is not yet a one click download.

## licence

mit, copyright aprewaturkson inc. see LICENSE.
