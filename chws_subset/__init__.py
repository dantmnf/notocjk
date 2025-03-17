import logging
from os import PathLike
from pathlib import Path
from concurrent.futures import Executor
import asyncio
from contextlib import AbstractAsyncContextManager, nullcontext

import chws_tool
import httpx
from fontTools import ttLib
from fontTools.varLib import instancer
from nototools import font_data, tool_utils
from tqdm import tqdm

## BEGIN: https://android.googlesource.com/platform/external/noto-fonts.git/+/refs/heads/android15-release/scripts/subset_noto_cjk.py
# Characters supported in Noto CJK fonts that UTR #51 recommends default to
# emoji-style.
EMOJI_IN_CJK = {
    0x26BD,  # ⚽ SOCCER BALL
    0x26BE,  # ⚾ BASEBALL
    0x1F18E,  # 🆎 NEGATIVE SQUARED AB
    0x1F191,  # 🆑 SQUARED CL
    0x1F192,  # 🆒 SQUARED COOL
    0x1F193,  # 🆓 SQUARED FREE
    0x1F194,  # 🆔 SQUARED ID
    0x1F195,  # 🆕 SQUARED NEW
    0x1F196,  # 🆖 SQUARED NG
    0x1F197,  # 🆗 SQUARED OK
    0x1F198,  # 🆘 SQUARED SOS
    0x1F199,  # 🆙 SQUARED UP WITH EXCLAMATION MARK
    0x1F19A,  # 🆚 SQUARED VS
    0x1F201,  # 🈁 SQUARED KATAKANA KOKO
    0x1F21A,  # 🈚 SQUARED CJK UNIFIED IDEOGRAPH-7121
    0x1F22F,  # 🈯 SQUARED CJK UNIFIED IDEOGRAPH-6307
    0x1F232,  # 🈲 SQUARED CJK UNIFIED IDEOGRAPH-7981
    0x1F233,  # 🈳 SQUARED CJK UNIFIED IDEOGRAPH-7A7A
    0x1F234,  # 🈴 SQUARED CJK UNIFIED IDEOGRAPH-5408
    0x1F235,  # 🈵 SQUARED CJK UNIFIED IDEOGRAPH-6E80
    0x1F236,  # 🈶 SQUARED CJK UNIFIED IDEOGRAPH-6709
    0x1F238,  # 🈸 SQUARED CJK UNIFIED IDEOGRAPH-7533
    0x1F239,  # 🈹 SQUARED CJK UNIFIED IDEOGRAPH-5272
    0x1F23A,  # 🈺 SQUARED CJK UNIFIED IDEOGRAPH-55B6
    0x1F250,  # 🉐 CIRCLED IDEOGRAPH ADVANTAGE
    0x1F251,  # 🉑 CIRCLED IDEOGRAPH ACCEPT
}
# Characters we have decided we are doing as emoji-style in Android,
# despite UTR #51's recommendation
ANDROID_EMOJI = {
    0x2600,  # ☀ BLACK SUN WITH RAYS
    0x2601,  # ☁ CLOUD
    0x260E,  # ☎ BLACK TELEPHONE
    0x261D,  # ☝ WHITE UP POINTING INDEX
    0x263A,  # ☺ WHITE SMILING FACE
    0x2660,  # ♠ BLACK SPADE SUIT
    0x2663,  # ♣ BLACK CLUB SUIT
    0x2665,  # ♥ BLACK HEART SUIT
    0x2666,  # ♦ BLACK DIAMOND SUIT
    0x270C,  # ✌ VICTORY HAND
    0x2744,  # ❄ SNOWFLAKE
    0x2764,  # ❤ HEAVY BLACK HEART
}
# We don't want support for ASCII control chars.
CONTROL_CHARS = set(tool_utils.parse_int_ranges("0000-001F"))
EXCLUDED_CODEPOINTS = frozenset(sorted(EMOJI_IN_CJK | ANDROID_EMOJI | CONTROL_CHARS))

## END: https://android.googlesource.com/platform/external/noto-fonts.git/+/refs/heads/android15-release/scripts/subset_noto_cjk.py

def ensure_parent_dir(path: PathLike):
    Path(path).parent.mkdir(parents=True, exist_ok=True)

def process_ttf_worker(in_ttf, out_ttf, temp_dir):
    """
    Apply CHWS patch to a font file.
    Remove a set of characters from font file's cmap table.
    Instantiate wght=400 as default instance for variable fonts.
    """
    chws_output = Path(temp_dir) / "intermediate_chws" / Path(in_ttf).name
    ensure_parent_dir(chws_output)
    print(f"  ADD_CHWS\t{in_ttf}")
    chws_tool.add_chws(in_ttf, chws_output)

    print(f"  SUBSET\t{chws_output}")
    font = ttLib.TTFont(chws_output)
    font_data.delete_from_cmap(font, EXCLUDED_CODEPOINTS)

    if 'fvar' in font:
        # drop VORG from font as it is optional and not handled in fontTools.varLib.instancer (yet)
        # ref: https://learn.microsoft.com/en-us/typography/opentype/spec/vorg
        print(f"  VFINST\t{chws_output}")
        del font['VORG']
        font['VVAR'].table.VOrgMap = None
        instancer.instantiateVariableFont(font, {'wght':(100, 400, 900)}, inplace=True, updateFontNames=False)

    print(f"  TTF\t{out_ttf}")
    ensure_parent_dir(out_ttf)
    font.save(out_ttf)


async def download_file(
    url: str, save_path_file_name: PathLike,
    actx: AbstractAsyncContextManager | None = None
) -> bool:
    async with (actx if actx is not None else nullcontext()):
        print(f"  FETCH\t{save_path_file_name}")
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url, follow_redirects=True) as response:
                if response.status_code != 200:
                    logging.error(f"Failed to download {url}")
                    return False
                with tqdm(
                    total=int(response.headers.get("content-length", 0)),
                    unit="B",
                    unit_divisor=1024,
                    unit_scale=True,
                ) as progress:
                    ensure_parent_dir(save_path_file_name)
                    with open(save_path_file_name, "wb") as f:
                        num_bytes_downloaded = response.num_bytes_downloaded
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
                            progress.update(
                                response.num_bytes_downloaded - num_bytes_downloaded
                            )
                            num_bytes_downloaded = response.num_bytes_downloaded
    return True


def unpack_ttc_worker(in_ttc: PathLike, index, out_file: PathLike):
    print(f"  UNTTC\t{in_ttc} -> {out_file}")
    ttc = ttLib.TTCollection(in_ttc)
    font = ttc[index]
    ensure_parent_dir(out_file)
    font.save(out_file)


async def unpack_ttc(executor: Executor, in_ttc: PathLike, out_dir: PathLike) -> list[Path]:
    loop = asyncio.get_event_loop()
    out_ttfs = []
    print(f"  UNTTC\t{in_ttc}")
    ttc = ttLib.TTCollection(in_ttc)
    futures = []
    for i in range(len(ttc)):
        ttf_out = Path(out_dir) / (Path(in_ttc).name + f"#{i}.ttf")
        out_ttfs.append(ttf_out)
        futures.append(loop.run_in_executor(executor, unpack_ttc_worker, in_ttc, i, ttf_out))
    await asyncio.gather(*futures)
    return out_ttfs


def pack_ttc(ttfs: list[PathLike], out_ttc: PathLike):
    print(f"  TTC\t{out_ttc}")
    ttc = ttLib.TTCollection()
    for ttf in ttfs:
        ttc.fonts.append(ttLib.TTFont(ttf))
    ensure_parent_dir(out_ttc)
    ttc.save(out_ttc)


def is_ttc(file_path: PathLike) -> bool:
    with open(file_path, "rb") as f:
        return f.read(4) == b"ttcf"


async def process_ttf(executor: Executor, in_ttf: PathLike, out_ttf: PathLike, temp_dir: PathLike):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(executor, process_ttf_worker, in_ttf, out_ttf, temp_dir)


async def process_ttc(executor: Executor, in_ttc: PathLike, out_ttc: PathLike, temp_dir: PathLike):
    loop = asyncio.get_event_loop()
    out_ttfs = []
    ttc = ttLib.TTCollection(in_ttc)

    async def unpack_and_process_ttf(in_ttc, index, ttf_out):
        input_ttf = Path(temp_dir) / "input_ttf" / (Path(in_ttc).name + f"#{index}.ttf")
        await loop.run_in_executor(executor, unpack_ttc_worker, in_ttc, index, input_ttf)
        await process_ttf(executor, input_ttf, ttf_out, temp_dir)

    coros = []
    for i in range(len(ttc)):
        ttf_out = Path(temp_dir) / "processed_ttf" / (Path(in_ttc).name + f"#{i}.ttf")
        out_ttfs.append(ttf_out)
        coros.append(unpack_and_process_ttf(in_ttc, i, ttf_out))

    await asyncio.gather(*coros)
    await loop.run_in_executor(executor, pack_ttc, out_ttfs, out_ttc)


async def process_font(executor: Executor, in_font: PathLike, out_font: PathLike, temp_dir: PathLike):
    if is_ttc(in_font):
        await process_ttc(executor, in_font, out_font, temp_dir)
    else:
        await process_ttf(executor, in_font, out_font, temp_dir)
