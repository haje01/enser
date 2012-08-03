CodeMirror.defineMode("enser", function(config, parserConfig) {
    var indentUnit = config.indentUnit;

    function inText(stream, state) {
        stream.eatSpace();
        var ch = stream.next();
        if (ch == "[") {
            openBracket(state);
            return "tag";
        } else if (ch == "]") {
            state.lastChar = codeMirror.getCursor(false);
            closeBracket(state);
            return "tag";
        } else if (ch == "#") {
            state.head = true;
            return "tag";
        } else if (ch == " ") {
            state.head = false;
        } else {
            if (state.head && stream.eatWhile(/\w/)) {
              state.head = false;
              return "builtin";
            }
            state.lastChar = codeMirror.getCursor(false);
            return "cm-variable-3";
        }
    }

    function openBracket(state) {
        state.bracketDepth += 1
    }

    function closeBracket(state) {
        state.bracketDepth -= 1;
    }

    return {
        startState: function() {
            return {tokenize: inText, bracketDepth: 0, startOfLine: true, tagName: null, context: null};
        },

        token: function(stream, state) {
            if (stream.sol()) {
                state.startOfLine = true;
            }
            if (stream.eatSpace()) return null;
            var style = state.tokenize(stream, state);
            state.startOfLine = false;
            return style;
        },

        indent: function(state, textAfter, fullLine) {
            var depth = state.bracketDepth;
            if (state.lastChar && textAfter == "]") {
                var start = state.lastChar;
                var end = codeMirror.getCursor(false);
                codeMirror.replaceRange("]", start, end);
                return null;
            }
            return depth * indentUnit;
        },

        electricChars: "]\n"
    };
});

CodeMirror.defineMIME("application/enser", "enser");

var enserKeywords = ("break case catch continue debugger default delete do else false finally for function " +
                  "if in instanceof new null return switch throw true try typeof var void while with").split(" ");

CodeMirror.enserHint = function(cm, symbol) {
    var getTokenAt = function() { return 'disabled'; }

    CodeMirror.simpleHint(cm, getHint);
}

function checkKR(txt)
{
    var vCnt = 0;

    for(i = 0; i < txt.length; i++)
    {
        if(txt.charCodeAt(i) >= 0 && txt.charCodeAt(i) <= 127)
        {
            // 영문 이나 숫자 타입
        } else {
            // 한글일때..
            vCnt++;
        }
    }

    if(vCnt > 0)
        return true;
    else
        return false;
}

var getHint = function(cm) {
    var cursor = cm.getCursor();
    var line = cursor.line;

    if(cursor.ch > 0) {
        var from, to;
        to = cursor;
        found:
        for (var l = line; l >= 0; --l) {
            var ch;
            if(l == cursor.line) {
                ch = cursor.ch;
            } else {
                ch = cm.lineInfo(l).text.length;
            }
            for (var c = ch; c >= 0; --c) {
                var pos = {line: l, ch: c};
                var token = cm.getTokenAt(pos);
                if (token.className == "tag") {
                    if (typeof from == 'undefined' && token.string == "#") {
                        from = {line: l, ch: c};
                        break found;
                    }
                }
            }
        }
        if (typeof from == 'undefined')
            return;
        var text = cm.getRange(from, to);
        requestAjax('/dictionary/' + text, null,
            function(html) {
                res = '<div class="page-header"><h1>Dictionary Result <small>사전 검색 결과</small></h1><div class="span9" style="margin-top: 20px; margin-left: 20px;">' + html + '</div></div>'
                $('#dic-result-div').empty();
                $('#corpus-result-div').empty();
                $('#dic-result-div').append(res);
                $('a[href^="popManager.nhn"]').replaceWith(function() {
                    return '<span>' + $(this).text() + '</span>';
                });
                $('img[src*="dicimg.naver.com"]').replaceWith();
                $('a[class="play2"]').replaceWith();
                $('a[href^="play2"]').replaceWith();
                var hasKR = checkKR(text);
                var search = hasKR ? 'dd p' : 'dd p.bg';
                var para = hasKR ? '<p>' : '<p class="bg">';
                $(search).replaceWith(function() {
                    var txt = $.trim($(this).contents().text());
                    if (!checkKR(txt)) 
                        return $(para).append($('<a href="javascript:void(0);" onclick="codeMirror.replaceSelection(\'' + txt.replace(/'/g, "\\'") +'\'); codeMirror.focus();">').append(txt));
                });
                if (!hasKR) {
                    $('dt').replaceWith(function() {
                        var txt = $.trim($(this).contents().text());
                        if(!checkKR(txt))
                            return $(para).append($('<a href="javascript:void(0);" onclick="codeMirror.replaceSelection(\'' + txt.replace(/'/g, "\\'") +'\'); codeMirror.focus();">').append(txt));
                    });
                }
                cm.setSelection({line: from.line, ch: from.ch - 1}, to);
                /*$('html, body').animate({
                    scrollTop: $('#corpus-result-div').offset().top
                    }, 600);*/
            },
            function() {
            }, 
            'text/html'
        );
    }
}

