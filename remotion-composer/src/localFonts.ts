import { loadFont } from "@remotion/fonts";
import { staticFile } from "remotion";

export const spaceGroteskFamily = "Space Grotesk";
export const playfairDisplayFamily = "Playfair Display";

void loadFont({
  family: spaceGroteskFamily,
  url: staticFile("fonts/space-grotesk-latin.woff2"),
  weight: "400 700",
  style: "normal",
});
void loadFont({
  family: playfairDisplayFamily,
  url: staticFile("fonts/playfair-display-latin.woff2"),
  weight: "400 900",
  style: "normal",
});
void loadFont({
  family: playfairDisplayFamily,
  url: staticFile("fonts/playfair-display-latin-italic.woff2"),
  weight: "400 700",
  style: "italic",
});
