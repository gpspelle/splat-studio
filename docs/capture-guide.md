# Recording guide

How to film an object so the reconstruction works on the first try. The same guide is
built into the app (the **Recording guide** button).

The software can only rebuild what the camera saw **clearly**, from **many angles**, in **one
continuous take**. Almost every failed run comes down to breaking one of those three.

![Recording guide in the app](images/recording-guide.png)

## 1. Pick the spot

- Room to walk all the way around the object, at a steady 1–2 m distance.
- Put it on a **textured surface**: a rug, newspaper, patterned cloth. COLMAP (the step that
  works out where the camera was) tracks texture, and a plain white table gives it nothing.
- **Matte objects work best.** Glass, mirrors, chrome and very glossy paint show reflections
  that move as you walk, which confuses the tracking. Cover or skip those parts.
- Nothing in view should move: no people, pets, curtains or screens.

## 2. Get the light right

- **Soft, even light**: overcast daylight, or a bright room with several lamps.
- Avoid harsh sun or a single spotlight. Hard shadows and highlights shift as you move.
- Don't change the lights during the take, and keep your own shadow off the object.

## 3. Set up the phone

| Setting | Why |
|---|---|
| 4K, 30 fps, landscape | Sharp detail, little motion blur. |
| Stay on the **1× lens**, never zoom | Zooming makes the phone switch between its cameras, but COLMAP assumes one camera for the whole video. |
| **Lock focus and exposure** (press and hold on the object until "AE/AF Lock" appears) | Otherwise the brightness and sharpness drift as you walk around. |
| Cinematic and Action mode **off** | Both crop and distort the image. |
| HDR Video off (Settings → Camera → Record Video) | Optional: HDR is converted automatically, but off gives the most faithful colours. |

## 4. Record: three loops, one take

```
        ③ high   — arms raised, looking down at the top
      ② eye level — straight on
    ① low        — knee height, looking slightly up
```

- Start recording and **don't stop** until you're done.
- Walk one full circle at each height. End each loop where it started, then rise gently
  into the next one. The overlap between loops is what joins them into one model.
- Walk **slowly**, about 2–3 seconds per step. At corners, turn your body; don't swing the phone.
- Keep the **whole object in frame**, filling about half to two-thirds of it.
- Total length: **1½–4 minutes**. Splat Studio picks about 250 frames evenly from the whole video.

## 5. Check before you leave

Scrub through the video. Every moment should be sharp, with no jumps, flicker, or people
walking through. Re-filming now takes a minute. Coming back later takes much longer.

## What Splat Studio tells you

- **On upload**, it checks the video's length, resolution, frame rate, HDR and orientation,
  and warns you before you spend time processing.
- **Right after the camera poses**, before training, a coverage ring shows where the cameras were
  around the object. A side you skipped, or filming from only one height, gives a warning with
  the moment in the video. A side nobody filmed stops the run before training, so you can film
  it again rather than wait; **Train anyway** continues with the same camera positions.
- **After a run**, if the camera track broke, it tells you **at which seconds of the video**
  (for example "around 0:42–0:51"). Look at those moments: usually a fast turn, motion blur,
  something passing in front, or the object leaving the frame. Re-film just that more slowly.
- It also flags when many frames were blurry (move slower, add light, lock focus).

## Avoid

- Glass, mirrors, chrome, very shiny paint
- Plain white tables and blank walls around the object
- **Turntables**: the object turns but the background doesn't, which breaks the tracking
- Cuts, pauses followed by fast pans, zooming
- Moving people, pets or curtains in view

A stand, a holder or your hand in the shot is fine if you need it: **Choose what to keep**
(beta) removes it before training. Make sure every part of the object is seen in some frames,
since what the holder covers in every frame can't be recovered.
