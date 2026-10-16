"""Extract the design's authoritative Markdown diagrams; optionally render locally."""
import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent
DOCUMENTS = ('README.md', 'MODULES.md', 'FLOWS.md')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT.parents[1] / 'runtime' / 'design')
    parser.add_argument('--jar', type=Path, help='Local PlantUML JAR; omitted means extract only')
    args = parser.parse_args()
    diagrams = {}
    for document in DOCUMENTS:
        source = (ROOT / document).read_text(encoding='utf-8')
        blocks = re.findall(r'^```plantuml\s*\n(.*?)^```\s*$', source, re.M | re.S)
        if source.count('```plantuml') != len(blocks):
            parser.error(f'{document}: unclosed PlantUML fence')
        for block in blocks:
            match = re.fullmatch(r'@startuml ([a-z][a-z0-9_]*)\n.*\n@enduml\s*', block, re.S)
            if not match or block.count('@startuml') != 1 or block.count('@enduml') != 1:
                parser.error(f'{document}: invalid diagram wrapper/name')
            name = match.group(1)
            if name in diagrams:
                parser.error(f'duplicate diagram name: {name}')
            diagrams[name] = block.rstrip() + '\n'
    if not diagrams:
        parser.error('no design diagrams found')
    args.out.mkdir(parents=True, exist_ok=True)
    sources = []
    for name, block in diagrams.items():
        path = args.out / f'{name}.puml'
        path.write_text(block, encoding='utf-8')
        sources.append(path.resolve())
    print(f'Exported {len(sources)} diagrams to {args.out.resolve()}', flush=True)
    if args.jar:
        if not args.jar.is_file():
            parser.error('--jar must point to a local PlantUML JAR')
        subprocess.run(['java', '-Djava.awt.headless=true', '-Dfile.encoding=UTF-8',
                        '-jar', str(args.jar.resolve()), '-charset', 'UTF-8', '-tsvg', '-failfast2',
                        *map(str, sources)], check=True)
        for path in sources:
            svg = path.with_suffix('.svg')
            if not svg.is_file() or '<svg' not in svg.read_text(encoding='utf-8'):
                raise RuntimeError(f'missing SVG: {svg}')
        print(f'Rendered {len(sources)} SVG diagrams', flush=True)


if __name__ == '__main__':
    main()
