
    sed "s#/PATH/TO/partyline#$PWD#g" docs/partyline.desktop > ~/.local/share/applications/partyline.desktop
    update-desktop-database ~/.local/share/applications 2>/dev/null



