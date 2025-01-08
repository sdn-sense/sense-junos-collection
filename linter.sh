#!/bin/bash
set -x

changed_files=$(git diff --name-only HEAD HEAD~1)
if [[ -z $changed_files ]]; then
    echo "No changes detected, checking all files in the repository recursively."
    changed_files=$(find . -type f)
fi

for fname in $changed_files; do
    if [[ $fname == *.py ]]
    then
        echo "Checking $fname with python linters"
        black "$fname"
        isort "$fname"
        pylint "$fname" --rcfile standarts/pylintrc
    fi
    if [[ $fname == *.yaml || $fname == *.yml ]]
    then
        echo "Checking $fname with yaml linters"
        yamllint "$fname"
    fi
    if [[ $fname == *.sh ]]
    then
        echo "Checking $fname with bash linter"
        bashlint "$fname"
    fi
done
