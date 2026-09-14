function chipLabelText(s) {
    const t = String(s == null ? "" : s).trim();
    if (!t) return "";
    const EXT = /\.(png|jpg|jpeg|gif|webp|bmp|tiff|heic|mp4|mov|webm|mkv|avi|wmv|flv|m4v|wav|mp3|ogg|flac|m4a|aac|opus)$/i;
    return t.replace(EXT, "").trim() || t;
}

const cases = [
    ["AI时代普通人该提升的3件事 (1).png", "AI时代普通人该提升的3件事 (1)"],
    ["ChatGPT Image 2026年9月4日", "ChatGPT Image 2026年9月4日"],
    ["角色1", "角色1"],
    ["v1.0", "v1.0"],
    ["file.png", "file"],
    ["a.b.jpg", "a.b"],
    ["image.webp", "image"],
    ["", ""],
    [null, ""],
    ["foo.MP4", "foo"],
    ["foo.wav", "foo"],
];
let fails = 0;
for (const [input, want] of cases) {
    const got = chipLabelText(input);
    if (got !== want) {
        console.log("FAIL:", JSON.stringify(input), "→", JSON.stringify(got), "want", JSON.stringify(want));
        fails++;
    } else {
        console.log("OK:", JSON.stringify(input), "→", JSON.stringify(got));
    }
}
process.exit(fails ? 1 : 0);
