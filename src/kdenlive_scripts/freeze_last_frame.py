# PYTHON_ARGCOMPLETE_OK
from path import DATA_DIRECTORY, RESULT_DIRECTORY
import argparse
import argcomplete
import xml.etree.ElementTree as ET
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from itertools import count
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
import sys

# note, this might be incompatible - different formats may be used
def timecode_to_seconds(timecode: str) -> float:
    # "HH:MM:SS.ms" -> seconds
    hh, mm, ss_ms = timecode.split(":")
    return int(hh) * 3600 + int(mm) * 60 + float(ss_ms)

def timecode_to_frames(duration: str, fps: int) -> int:
    # convert "HH:MM:SS.ms" into frames
    frames = round(fps * timecode_to_seconds(duration)) # note, for non <blank> you need to add +1
    return frames

def frames_to_time_code(duration: int, fps: int) -> str:
    total_ms = round(duration * 1000 / fps)
    hh, rest = divmod(total_ms, 3_600_000)
    mm, rest = divmod(rest, 60_000)
    ss, ms = divmod(rest, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"

# (fps, nb_frames) per source clip; probing is a subprocess, so only once per file
_clip_probe_cache: dict[str, tuple[float, int]] = {}

def probe_clip(clip_path: str) -> tuple[float, int]:
    """(fps, nb_frames) of a clip's first video stream, cached per path.

    AI-GENERATED (deepseek-v4-flash, 2026-09-27, human review: skimmed).
    Context: ffprobe's documented output format (`r_frame_rate`, `nb_frames`,
    `format=duration`, `-of default=nw=1:nk=1`) plus the source clips this
    project actually references (30fps Pixel HEVC). The `nb_frames` fallback is
    a defensive branch -- no file here has been observed reporting "N/A".
    """
    if clip_path not in _clip_probe_cache:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=r_frame_rate,nb_frames',
             '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', clip_path],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        rate, nb_frames, duration = out[0], out[1], out[2]
        num, den = rate.split("/")
        src_fps = float(num) / float(den)
        # nb_frames is "N/A" on some containers; the last frame start is then
        # floor(duration * fps) - 1
        count = int(nb_frames) if nb_frames.isdigit() else int(float(duration) * src_fps)
        _clip_probe_cache[clip_path] = (src_fps, count)
    return _clip_probe_cache[clip_path]

def frame_seek_time(timecode: str, clip_path: str) -> str:
    """Seek value that lands on the source frame the timeline shows at `timecode`.

    ffmpeg's `-ss t` with `-frames:v 1` returns the first frame whose
    presentation time is >= t, so a target inside the last frame of the file
    yields nothing at all (exit code 0, no output), and a target off a frame
    boundary lands on the next frame. Aiming at the start of the wanted frame
    fixes both: the frame at time t is floor(t * src_fps), clamped to the last
    frame that exists. Seconds are given to microseconds, because a clip is not
    always an exact number of project frames long.

    AI-GENERATED (deepseek-v4-flash, 2026-09-27, human review: skimmed).
    Context: measured ffmpeg behaviour (`-ss t` with `-frames:v 1` yields the
    first frame whose pts >= t; past the last frame's start it writes nothing and
    still exits 0) against this project's 25fps profile and 30fps sources, with
    results checked by md5 against frames extracted via `select=eq(n,K)`.
    """
    src_fps, nb_frames = probe_clip(clip_path)
    frame = min(int(timecode_to_seconds(timecode) * src_fps), nb_frames - 1)
    #* Note, this might not work with other time codes; idk if kdenlive supports only HH:MM:SS.MS
    return f"{frame / src_fps:.6f}"

def ffmpeg_job(args):
    seek, clip_path, frame_name = args
    subprocess.run(['ffmpeg', '-y', '-ss', seek, '-i', clip_path, '-frames:v', '1', '-update', '1', frame_name], check=True)
    # ffmpeg exits 0 when it encodes nothing ("Output file is empty"), so the
    # exit code alone does not tell us the frame was written
    if not Path(frame_name).exists():
        raise RuntimeError(f"ffmpeg wrote no frame for {clip_path} at -ss {seek}")
    return frame_name

def resolve_resource(resource: str, project_path: Path, mlt_root: str | None) -> Path:
    """Absolute path of a <property name="resource">, for handing to ffmpeg/ffprobe.

    MLT builds the path by concatenating <mlt root="..."> with the resource and
    interpreting the result against the *process working directory*, so the same
    project resolves differently depending on where it is run from:

        root="."      + resource="video.webm"        -> ./video.webm
        root="./data" + resource="./video.webm"      -> ./data/./video.webm

    Both forms appear in practice: Kdenlive writes whatever was true of its own
    cwd when the project was saved, and resources generated by other tools are
    sometimes relative to the project file instead. Rather than pick one reading,
    the candidates below are tried in order and the first existing path wins, so
    a project loads the same way from any directory. Absolute resources and an
    absolute media root pass straight through, as in the real project.

    AI-GENERATED (deepseek-v4-flash, 2026-09-27, human review: skimmed).
    Context: MLT's `root` concatenation, measured with melt on
    data/example_project.kdenlive (root="." loaded only with cwd=data/) and
    data/example_project4.kdenlive (root="./data" loaded only with cwd=repo
    root). An earlier version resolved a relative root against the project
    directory only, which turned root="./data" + "./video.webm" into
    data/data/video.webm -- the candidate list exists because neither reading
    covers both files.
    """
    path = Path(resource)
    if path.is_absolute():
        return path

    project_dir = Path(project_path).resolve().parent
    root = Path(mlt_root) if mlt_root else Path()
    if root.is_absolute():
        candidates = [root / path]
    else:
        candidates = [
            project_dir / root / path,  # root relative to the project (root=".")
            Path.cwd() / root / path,   # root relative to the cwd (root="./data")
            project_dir / path,         # resource relative to the project
            Path.cwd() / path,          # resource relative to the cwd
        ]

    tried = []
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in tried:          # keep the error message short
            continue
        tried.append(candidate)
        if candidate.exists():
            return candidate

    listing = "\n  ".join(str(c) for c in tried)
    raise FileNotFoundError(f"{resource} ({mlt_root=}) not found, tried:\n  {listing}")


def get_next_kdenlive_id(root: ET.Element) -> int:
    used = [
        int(p.text)
        for p in root.iter("property")
        if p.get("name") == "kdenlive:id" and (p.text or "").isdigit()
    ]
    return max(used, default=0) + 1

def run(args) -> None:
    project_path = args["input_project"]
    project = ET.parse(project_path)
    root = project.getroot()
    fps = int(root.find("profile").get("frame_rate_num"))
    playlists = {playlist.get("id"): playlist for playlist in root.findall("playlist")}
    playlist = playlists[args["playlist_id"]]
    chains = {chain.get("id"):chain for chain in root.findall("chain")}
    next_kdenlive_id = get_next_kdenlive_id(root)
    
    last_entry = None
    new_timeline = []
    ffmpeg_jobs_args = []
    
    gen_dir = args["output_frames_dir"]
    Path(gen_dir).mkdir(parents=True, exist_ok=True)
    i = 0
    
    for element in playlist:
        if element.tag == 'blank' and last_entry is not None:
            chain_id = last_entry.get('producer')
            clip_path = resolve_resource(
                chains[chain_id].findtext("property[@name='resource']"),
                project_path,
                root.get("root"),
            )
            out_time = last_entry.get('out')
            length = element.get("length")
            n_frames = timecode_to_frames(length, fps)
            new_out = frames_to_time_code(n_frames - 1, fps)
            
            # STEP 1: extract frame
            frame_name = f"{gen_dir}/{chain_id}_freezeframe{i}.png"
            ffmpeg_jobs_args.append((frame_seek_time(out_time, clip_path), clip_path, frame_name))
            i+= 1
            
            # STEP 2: add to project
            producer_id = f"{chain_id}_freezeframe{i}"
            zero_frame = "00:00:00.000"
            producer = ET.Element(
                "producer",
                {"id":producer_id, "in": zero_frame, "out": new_out}
            )
            
            properties = [
                ("length", str(n_frames)),
                ("eof", "pause"), # when reach the end of sequence, pause the frame
                ("ttl", str(fps)), # idk, what it means, probably needs to be FPS
                ("resource", frame_name),
                ("mlt_service", "qimage"),
                ("format", "1"), # idk, what this means; the file format is under-documented
                ("meta.media.progressive", "1"),
                ("seekable", "1")
            ]
            
            for name, value in properties:
                ET.SubElement(producer, "property", {"name": name}).text = value
            
            copy_properties = [
                "aspect_ratio",
                "meta.media.width", 
                "meta.media.height",
                "xml" # everywhere is: "was here", just copy it
            ]
            
            for name in copy_properties:
                value = chains[chain_id].findtext(f"property[@name='{name}']")
                ET.SubElement(producer, "property", {"name": name}).text = value
            
            ET.SubElement(producer, "property", {"name": "kdenlive:id"}).text = str(next_kdenlive_id)
            
            root.insert(list(root).index(playlist), producer)
            
            # STEP 3: append to timeline
            new_element = ET.Element(
                "entry",
                {"in": zero_frame, "out":new_out, "producer":producer_id}
            )
            ET.SubElement(new_element, "property", {"name": "kdenlive:id"}).text = str(next_kdenlive_id)
            new_timeline.append(new_element)
            
            next_kdenlive_id += 1
        
        elif element.tag == 'blank':
            # keep the leading blank space
            new_timeline.append(element)    
        
        elif element.tag == 'entry':
            last_entry = element
            new_timeline.append(element)
            
    
    # STEP 3.5 replace timeline
    playlist[:] = new_timeline
    
    # STEP 5: run ffmpeg
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(ffmpeg_job, j) for j in ffmpeg_jobs_args]
        
        with tqdm(total=len(futures), desc="frames") as bar:
            for f in as_completed(futures):
                try:
                    f.result()
                except (subprocess.CalledProcessError, RuntimeError) as e:
                    tqdm.write(f"failed: {e}")
                bar.update(1)
    
    # STEP 6: save as new project
    # Elements built here have no text/tail, so they serialize as one long line
    # inside otherwise indented XML. ET.indent rewrites whitespace-only
    # text/tail as one space per level, which is how Kdenlive writes these files
    # (byte-identical on an untouched project), and it leaves elements whose text
    # is real content -- properties with embedded newlines -- alone.
    ET.indent(project, space=" ")
    project.write(args["output_project"], encoding="utf-8", xml_declaration=True)
    print("Finished!")

def inquire_args(args):
    if "input_project" not in args:
        args["input_project"] = inquirer.filepath("Enter kdenlive project path:").execute()
    
    project = ET.parse(args["input_project"])
    root = project.getroot()
    playlists = {playlist.get("id"): len([elm for elm in playlist if elm.tag == "entry"]) for playlist in root.findall("playlist")}
    
    if "playlist_id" not in args:
        args["playlist_id"] = inquirer.select(message="Select playlist (track) to modify:",
                        choices=[Choice(value=id, name=f"{id} ({n_clips} clips)") for id, n_clips in playlists.items()]
                        ).execute()
        
    if "output_project" not in args:
        args["output_project"] = inquirer.filepath("Output project:", default=args["input_project"]).execute()
        
    if "output_frames_dir" not in args:
        args["output_frames_dir"] = inquirer.filepath("Generate frames to:", default=f"{args["output_project"]}_frames/").execute()
        
    if args["output_project"] == args["input_project"] and not args.get("assume_yes"):
        res = inquirer.confirm(message="This will override project, MAKE SURE YOU'VE MADE BACKUP, proceed?", default=True).execute()
        if not res:
            sys.exit(0)
    return args
    
def playlist_ids(project_path: Path) -> list[str]:
    """Playlist ids of a project, or [] if it cannot be read.

    AI-GENERATED (deepseek-v4-flash, 2026-09-27, human review: skimmed).
    Context: this project's own XML layout -- playlists are top-level
    <playlist id="..."> siblings of the tractors -- read with ElementTree's
    findall, plus the fact that main_bin is one of them.
    """
    try:
        root = ET.parse(project_path).getroot()
    except (OSError, ET.ParseError):
        return []
    return [playlist.get("id") for playlist in root.findall("playlist") if playlist.get("id")]


def complete_playlist(prefix, parsed_args, **kwargs):
    """argcomplete completer: playlist ids read from the project named on the line.

    AI-GENERATED (deepseek-v4-flash, 2026-09-27, human review: skimmed).
    Context: argcomplete's custom-completer docs -- the
    `(prefix, parsed_args, **kwargs)` signature, assigned to `option.completer`
    in build_parser -- plus this project's context, that the ids can only come
    from a project already given earlier on the command line.
    """
    project_path = getattr(parsed_args, "input_project", None)
    if not project_path:
        return []
    return [pid for pid in playlist_ids(project_path) if pid.startswith(prefix)]


def build_parser() -> argparse.ArgumentParser:
    """CLI surface: every argument mirrors one of the InquirerPy prompts.

    AI-GENERATED (deepseek-v4-flash, 2026-09-27, human review: rewritten docs for clarity).
    Context: argparse's docs, plus this project's context -- the prompts already
    written in inquire_args() (input project, playlist, output project, frames
    dir, overwrite confirm) mapped one-to-one onto flags, so anything omitted on
    the command line is still asked for interactively.
    """
    parser = argparse.ArgumentParser(
        prog="freeze-last-frame",
        description=(
            "In selected track, fill every gap with last clip frame. (\"last frame freezing\")"
        ),
    )
    parser.add_argument(
        "input_project",
        nargs="?",
        type=Path,
        help="source .kdenlive project (prompted if omitted)",
    )
    playlist = parser.add_argument(
        "--playlist", "-p",
        dest="playlist_id",
        metavar="ID",
        help="id of the <playlist> holding the track to process, e.g. playlist4 (prompts list selection if omitted)",
    )
    playlist.completer = complete_playlist
    parser.add_argument(
        "--output", "-o",
        dest="output_project",
        type=Path,
        help="where to write the patched project (default: override project)",
    )
    parser.add_argument(
        "--frames-dir", "-d",
        dest="output_frames_dir",
        type=Path,
        help="directory for the extracted PNGs (prompted if omitted)",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="overwrite an existing output project without asking",
    )
    return parser


def main() -> None:
    parser = build_parser()
    argcomplete.autocomplete(parser)
    parsed = vars(parser.parse_args())
    assume_yes = parsed.pop("yes")
    # keys that were not given stay unprompted, so inquire_args() asks for them
    args = {name: value for name, value in parsed.items() if value is not None}
    args["assume_yes"] = assume_yes
    run(inquire_args(args))