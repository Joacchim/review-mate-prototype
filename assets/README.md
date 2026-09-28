# Artwork

`reviewmate-icon.svg` is the master. Everything else is derived from it.

The favicon the server actually hands a browser is **`review_mate/web/favicon.ico`**, not a file
here: `review_mate/` is what the wheel ships (`packages = ["review_mate"]`), so a copy outside it
would be missing from every install. There is deliberately no second copy of the `.ico` in this
directory — two binaries of the same image drift, and the one that is served should be the one
anybody looks at.

To regenerate it after changing the SVG (16–256px, the sizes Windows and Linux desktops ask for):

```bash
magick assets/reviewmate-icon.svg -define icon:auto-resize=256,128,64,48,32,16 \
  review_mate/web/favicon.ico
```
