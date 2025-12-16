# Bash completion for shuttle
# Source this file or drop in /etc/bash_completion.d/

_shuttle_sessions() {
    # Get session names from tmux
    tmux ls 2>/dev/null | cut -d: -f1
}

_shuttle_briefs() {
    # Get open brief IDs from skein
    skein folios ledger --type brief --status open 2>/dev/null | grep -oE 'brief-[a-z0-9-]+' | head -20
}

_shuttle() {
    local cur prev words cword
    _init_completion || return

    local commands="status watch ls list board b kill k peek p send go g split s vsplit vs unsplit ground help"

    # If completing first argument (the command)
    if [[ $cword -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$commands" -- "$cur"))
        return
    fi

    # Get the command (first positional arg after shuttle)
    local cmd="${words[1]}"

    case "$cmd" in
        board|b|kill|k|peek|p|send)
            # These commands take session names/numbers
            local session_pos=2
            # Check for --headless flag shifting position
            for ((i=2; i<cword; i++)); do
                if [[ "${words[i]}" == "--headless" ]]; then
                    ((session_pos++))
                fi
            done

            if [[ "$cmd" == "send" ]]; then
                # send takes session then message - only complete session
                if [[ $cword -eq $session_pos ]]; then
                    COMPREPLY=($(compgen -W "$(_shuttle_sessions)" -- "$cur"))
                fi
            elif [[ $cword -eq $session_pos ]]; then
                # Complete --headless or session names
                if [[ "$cur" == -* ]]; then
                    COMPREPLY=($(compgen -W "--headless" -- "$cur"))
                else
                    COMPREPLY=($(compgen -W "$(_shuttle_sessions)" -- "$cur"))
                fi
            fi
            ;;

        go|g|split|s|vsplit|vs)
            # These commands take brief IDs and optional -d/-p/--headless flags
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "-d --dir -p --project --headless" -- "$cur"))
            elif [[ "$prev" == "-d" || "$prev" == "--dir" ]]; then
                # Complete directories
                _filedir -d
            elif [[ "$prev" == "-p" || "$prev" == "--project" ]]; then
                # Complete project names from ~/projects
                local projects
                if [[ -d "$HOME/projects" ]]; then
                    projects=$(ls -1 "$HOME/projects" 2>/dev/null)
                fi
                COMPREPLY=($(compgen -W "$projects" -- "$cur"))
            else
                # Complete brief IDs
                COMPREPLY=($(compgen -W "$(_shuttle_briefs)" -- "$cur"))
            fi
            ;;

        status|watch|ls|list|unsplit|ground|help)
            # No arguments to complete
            ;;
    esac
}

complete -F _shuttle shuttle
