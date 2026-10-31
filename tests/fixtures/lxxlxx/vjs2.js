document.writeln("<html>");
document.writeln("<head>");
document.writeln("<meta charset=\'utf-8\'>");
document.writeln("<meta name=\'RATING\' content=\'RTA-5042-1996-1400-1577-RTA\'/>");
document.writeln("<meta name=\'robots\' content=\'nofollow\' />");
document.writeln("<script type=\'text/javascript\' src=\'/imgs/video6.2.5.js/6.2.5/video.min.js\' rel=\'nofollow\'></script>");
document.writeln("<script type=\'text/javascript\' src=\'/imgs/play.js/video-js-resolution-switcher/video-js-resolution-switcher.js\' rel=\'nofollow\'></script>");
document.writeln("<script type=\'text/javascript\' src=\'/imgs/play.js/jquery-1.11.2.min.js\' rel=\'nofollow\'></script>");

document.writeln("<link href=\'/imgs/video6.2.5.js/css/video-js.css\' rel=\'stylesheet\'>");
document.writeln("<link href=\'/imgs/video6.2.5.js/css/style_videojs.css\' rel=\'stylesheet\'>");
document.writeln("<link href=\'/imgs/play.js/video-js-resolution-switcher/video-js-resolution-switcher.css\' rel=\'stylesheet\'>");
document.writeln("<style>");
document.writeln("	#player-container {position: relative; display: block; width: 680px; height: 520px; text-align: center; }");
document.writeln("	#player {width: 680px; height: 520px; position: absolute; background-color: #000000; background-repeat: none;}");
document.writeln("	#player-advertising {position: absolute; z-index: 999999; background: #ffffff; padding: 0 2px; text-align: center; filter: alpha(opacity=90); moz-opacity: 0.90; opacity: 0.90; border-radius: 5px;}");
document.writeln("	#player-advertising p { text-align: center; font-size: 14px; display: block; padding: 2px 0; margin: 0;}");
document.writeln("	#player-advertising .btn-adv {font-size: 16px; font-weight: bold; background: #317BB9; -webkit-border-radius: 10; -moz-border-radius: 10; border-radius: 10px; font-family: Arial; color: #ffffff; padding: 5px 10px 5px 10px;text-decoration: none; display: block; margin: 5px;}	");
document.writeln("</style>");

document.writeln("<script type=\'text/javascript\'>");
document.writeln("function adv_pos()");
document.writeln("{");
document.writeln("    var adv         = $(\'#player-advertising\');");
document.writeln("    if (adv.length) {");
document.writeln("  		var container	= $(\'#player-container\');");
document.writeln("  		var width		= container.width();");
document.writeln("  		var height		= container.height();");
document.writeln("        if (width > 310 && height > 310) {");
document.writeln("            var pos_top     = (height-300)/2;");
document.writeln("            var pos_left    = (width-330)/2;");
document.writeln("            adv.css(\'top\', pos_top + \'px\');");
document.writeln("            adv.css(\'left\', pos_left + \'px\');");
document.writeln("        } else {");
document.writeln("            adv.css(\'top\', 0);");
document.writeln("            adv.css(\'left\', 0);");
document.writeln("        }");
document.writeln("    }");
document.writeln("}");
document.writeln("$(document).ready(function() {");
document.writeln("    adv_pos();");
document.writeln("    $(window).resize(function() {adv_pos();});");
document.writeln("});");
document.writeln("</script>");

document.writeln("</head>");
document.writeln("<body>");


document.writeln("<div id=\'player-container\' oncontextmenu=\'return false\' onselectstart=\'return false\'>");
document.writeln("	<div id=\'player-advertising\'>");
document.writeln("<p>Advertisement</p>");
document.writeln("<script src=\'//zd-en.gotojs.xyz/video-en-on-pause_2.js\'></script>");
document.writeln("<a href=\'#close\' class=\'btn btn-default btn-adv\'>Close & Play</a>");
document.writeln("</div>	<video id=\'player\' class=\'video-js vjs-default-skin\' controls preload=\'auto\' width=\'100%\' height=\'100%\' poster=\'/imgs/files/190/202608231907136775.jpg\'>");

document.writeln("	            <source src=\'//m4-a-mail-en2p.gotojs.xyz/b_rE9bq7/move/202608/18908_piS9d.mp4\' type=\'video/mp4\'/>");

document.writeln("            </video>");
document.writeln("</div>");


document.writeln("<script type=\'text/javascript\'>");
document.writeln("videojs.options.flash.swf = \'/imgs/play.js/video-js/video-js.swf\';");
document.writeln("$(document).ready(function() {");
document.writeln("	var player = videojs(\'player\');	");
document.writeln("	player.videoJsResolutionSwitcher({\'default\': 240, \'dynamicLabel\': true});");
document.writeln("	    videojs(\'player\').ready(function() {this.on(\'play\', function() {$(\'#player-advertising\').hide();});this.on(\'pause\', function() {$(\'#player-advertising\').show();});});");
document.writeln("    $(\'.btn-adv\').click(function(e) {e.preventDefault();$(\'#player-advertising\').hide();videojs(\'player\').ready(function() {this.play();});});");
document.writeln("        ");
document.writeln("});");
document.writeln("</script>");


document.writeln("<script language=\'JavaScript\' src=\'//statistics2.gotojs.xyz/stat/?vId=18908\'></script>");

document.writeln("</body>");
document.writeln("</html>");