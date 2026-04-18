#!/usr/bin/env fish

# Fish wrapper for the Bash installer.
set script_dir (cd (dirname (status filename)); and pwd)
bash "$script_dir/install_script.sh" $argv
