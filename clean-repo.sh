#!/usr/bin/env zsh
cd "${0:a:h}"
VSCODE_REPO=work/vscode

function show_help() {
    echo "Usage: ${0:a:t} [--deep] [-y]"
    echo
    echo "Reset the vscode repository to a pristine state of the base commit."
    echo
    echo "Options:"
    echo "  --deep  Also remove files ignored by .gitignore (build artefacts, node_modules)"
    echo "  -y      Skip the confirmation prompt"
    exit 1
}

if ! [ -d $VSCODE_REPO/.git ]
then
    echo "vscode repository does not exist. Nothing to do."
    exit
fi

for arg in "$@"; do
    case "$arg" in
        --deep) DEEPCLEAN=1 ;;
        -y) SKIP_CONFIRM=1 ;;
        *) show_help ;;
    esac
done

if [[ $DEEPCLEAN -eq 1 ]]
then
    echo "This will remove EVERYTHING from the 'vscode' repository, including build artefacts, node-modules, and untracked files."
else
    echo "This will clean all uncommited changes, including UNTRACKED files."
fi

if [[ $SKIP_CONFIRM -ne 1 ]]; then
    read ANSWER\?"Type 'yes' to proceed: "
    if [[ "$ANSWER" != 'yes' ]]; then
        echo "Operation cancelled."
        exit
    fi
fi

echo "Cleaning repository ..."
cd $VSCODE_REPO
git submodule deinit --all -f 2>/dev/null
if [[ $DEEPCLEAN -eq 1 ]]
then
    echo "Cleaning ..."
    rm -rf $(ls -A | grep -v '^.git$')
    echo "Restoring from commit ..."
    git restore .
else
    git reset --hard HEAD
    git clean -fd
fi
