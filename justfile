# Where `just install` puts the `datafile` command.
bindir := env('XDG_BIN_HOME', env('HOME') / '.local/bin')

[private]
default:
    @just --list

# Put `datafile` on your PATH
install:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p '{{bindir}}'
    chmod +x datafile.py
    ln -sfn "$PWD/datafile.py" '{{bindir}}/datafile'
    echo "linked {{bindir}}/datafile -> $PWD/datafile.py"
    case ":$PATH:" in
        *":{{bindir}}:"*) datafile --version ;;
        *) echo "{{bindir}} is not on your PATH. Add to your shell profile:"
           echo '  export PATH="{{bindir}}:$PATH"' ;;
    esac

# Remove the `datafile` command
uninstall:
    #!/usr/bin/env bash
    set -euo pipefail
    link='{{bindir}}/datafile'
    if [ ! -L "$link" ] && [ ! -e "$link" ]; then
        echo "not installed: $link"
    elif [ "$(readlink "$link" || true)" = "$PWD/datafile.py" ]; then
        rm "$link"
        echo "removed $link"
    else
        echo "refusing to remove $link: not a link to $PWD/datafile.py" >&2
        exit 1
    fi
