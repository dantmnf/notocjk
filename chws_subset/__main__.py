import argparse
import concurrent.futures
import asyncio
import shutil
import urllib.parse
from pathlib import Path

from . import download_file, process_font

DEFAULT_DOWNLOADING_FONTS = {
    "NotoSerifCJK-VF.otf.ttc": "https://github.com/notofonts/noto-cjk/raw/refs/heads/main/Serif/Variable/OTC/NotoSerifCJK-VF.otf.ttc",
    "NotoSansCJK-VF.otf.ttc": "https://github.com/notofonts/noto-cjk/raw/refs/heads/main/Sans/Variable/OTC/NotoSansCJK-VF.otf.ttc",
}


async def main():
    parser = argparse.ArgumentParser(
        description="Download and patch Noto fonts with CHWS"
    )
    parser.add_argument("--url", help="URL to download and patch", default=None)
    args = parser.parse_args()
    build_module = True
    if args.url:
        urls = [args.url]
        build_module = False
    else:
        urls = DEFAULT_DOWNLOADING_FONTS.values()

    download_sem = asyncio.Semaphore(2)
    temp_dir = Path("temp")

    executor = concurrent.futures.ProcessPoolExecutor(max_tasks_per_child=1)

    async def download_and_process_file(url: str):
        base_file_name = urllib.parse.urlparse(url).path.split("/")[-1]
        input_file = temp_dir / "input" / base_file_name
        await download_file(url, input_file, download_sem)
        await process_font(executor, input_file, Path("system/fonts") / base_file_name, temp_dir)

    futures = [download_and_process_file(url) for url in urls]

    if build_module:
        futures.append(download_file("https://github.com/topjohnwu/Magisk/raw/master/scripts/module_installer.sh", "META-INF/com/google/android/update-binary"))

    await asyncio.gather(*futures)
    executor.shutdown()
    shutil.rmtree(temp_dir)


if __name__ == "__main__":
    asyncio.run(main())
