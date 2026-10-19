"""Frozen synthetic SDPC for packaged, decoder-only release checks.

Generated from tests/sdpc_fixture.py::build_sdpc(width=97, height=65,
tile_width=64, tile_height=48, levels=1), using development PyAV 16.0.1 and
libx265. Contains only mathematical RGB patterns, no patient or slide data.
The shipped test never imports the generator or needs an HEVC encoder.
RGB digests were computed from independently decoded tiles by the generator.
"""

import base64
import hashlib
import zlib

WIDTH, HEIGHT = 97, 65
TILE_WIDTH, TILE_HEIGHT = 64, 48
MPP = 0.37
ROI = (55, 39, 42, 26)  # crosses both tile seams and reaches the cropped edge
SOURCE_SHA256 = "06362d570d7c6c4fc5bda3c393995f705c2015c921341c9d9401e03d1b27e7a5"
RGB_SHA256 = "e09891028baa527478c0cfc0a033afb556f4818b9a1ef04714da8b7d3d856090"
ROI_RGB_SHA256 = "815de4da33f2548b06475297254670f915795e714c2dc36f4e221dfa66892e6f"

_DATA = (
    "c-rk(d05j}*QW@hETSTc2ugHD0U_8T1w<+c3IehSuAq)-nzq5V2}ugpg-sb3#8FWMml;qLS8xMCMHB@W6h}u#8Ak"
    "zETtS=>w}I~_E$F<@_x<rb@AJNYHBXXz?sjsP-#NJ_dMd3^F>;K-Q8+<h0Ipo{_>MOC$HfB=0-g!r@dFS4tGPjuQ"
    "9=GJ;ae2cw8*OHwy&d(3wu@SS|fx1=QSyK<X7AOx9jQ^bHCugMLO}=4;bp6cd&NF7d2A4g|i1edxL-f`o`SAzzaO"
    "KW(Ee^O|@3?<C*h1J9+aB40;%>V_)DNdcu&k7|)-_`<ID4XxYHnf`Y0iDJg89do5>|PUM}5PqhR@IM2AV>#}m$ao"
    "?oGI_U1n=>;9hRiDEbx_Jm8N3j}LNFX<X&<TPecUOV402W9D0#7mK9UF$AP((B|Yi=y6KvXhFAav!s^5@2SxI<!`"
    "nG1P=x3hrHz1_eYaG2~0xQ?DYX+{8q5)uprLjeS-p>Ua;f!z3lF)(;}L5`>s6sA@H&=3@sh)|dd6s%$w%Nd6B9zB"
    "|s&6S{JV1ytUoKVr;5K5|XiD;}BB&84~1~YOBLnO3F0IAeU1SSbWL5oy`3WHPxoJf*vLn?s`T0+~<DvE7s8dpLJL"
    "Ix|a1WX}<1*|?2jAY~@F9=tWY6eDW60nw%-~*!tx-kl)MIP=8-Ptt^Mkx>!6Y(L2U=Rh&2}Of0#}dXRD4^v)ZJ5C"
    "^3b0bkC9qfm1}RL+3QfR>LO~?LKx7ytR8oRcA}SOE(<Fv4upB6dQ8a|8RM<i>w_Gh@8;lTZ8=7t8lmrMPB_KIM%U"
    "NMCB}q^~9tENS+rbDyVk%fhkdP8r!3!`A5FLD8N4N?35SW7%1hD{NKM6L7yHydxhy)=SK!GobVc<qYiA!_~U@?xc"
    "izxITySr(aR^TYdhDI?JhN^XZ<T%a1T|*FsNEF&}nq=3r6cCI;fU%{(-f{?K)S@wNob;#~rtw9XNZ`RPqcD`=?4k"
    "s^Aq!zfHwzX6lJgL(^{GHN0JwGyC~zgtfGw;75+Ys)1U@9usDO?rOaWb7`TQ=hfIdg!GL;<HLW-dm-~eQlmNBaXr"
    "TwT@9pF+LjN>XyqFn)jE}@d?<e-%V!N_qCRIKPqjFMp@H=R^Kb6U$p+h;*cLrNBFv|7xyU;_w9OeFOD;&EjHhk31"
    "YBx<fB*Wx}AW8;tfLTUk}LKK=s7)%2fk~m635=^Wh&;=sCw^nh4&@~)Lb+f=;KvEi00Q2Js7&{FF!VpTKfjN^<+*"
    "^tOH)|E|G8W8^akfzd$k4O`qiHR&y4SHFU?_=>kpw^&Q7{_7pQJUcPuy6aXlWw8+e2Ujuo<om{cg^vIG!Y=oGDSN"
    "o1{<?N=d<pROIIF%7@}nLP-Kn9)Kii1|vo8z*7iA!-*8ma5m?9losANNg*H~i3G0hd<co>d?Vs0;9g861K{eC3c#"
    "DfFlZ4-NdOIMl@9-49gc$hBBr2tSvO)?El^m-0tUzdFd_07%M!TiD>%Uk;sl6BScDM?Ai7b6olO9MB)Ig!h=h<97"
    "eHk~K~N-xD}l3saaFVwAk$4d3|3P3LbfNT2qgl%$|4^J#z!=8BCe7Ui7W>N2nZKG7kLVhVp-S$ta7BAP{`&_H<%q"
    "E36KJ4F-V;WY8<3Huz`g+NJ?xUWGHP%mk+qq2a-A*Pr@WgclCe+0o;L(3eJjMQ%d$H0h0(#Ygd8MuH2zZK(2gY7Y"
    "~3d38r8Wz?#E1>mK&!!9{|E67ahTb~)RT{G*3UC^oCGwgp(maEOZ#8fU$)rm^lM(LGEfAOQZ4IR$}8{$&n0Oo4$Y"
    "KpBG4{s;hOyYt7FF80-|G67gjM}t%d12=))tX5yA2sTtvF3Cv=-8hlRSxo|xq;7k+XpDx`LWI<?fe${wx)D*cVC)"
    "i4;L5S-&WGLcMFZ~wEm-rg*2EG}U}$b4fit2M$H0LCIG6i(u)(5&wV)KRfzj<fg#GOUPl@<n{rz<4gV|uk7#iZv4"
    "eXzYU>HKlq7-9~6%>a%5Q&&pSr8Y%5Wp8-;KG3ir5PzFnwUbr<VFewM<t#k@4&nSd1LT6TeZo+dG2{|)Vg2%93%H"
    "{*>+-lezx={^g@>Ze&Lz}Q=eWC&5w0F8zM~%w)V}pNo?hRcvv2~cH!`T0pWhRkC#7vw7LxmwStFY&XX&jC0TelFA"
    "-E)gy$WceCy`0vhW`LTC;6uD1TKRSl#@6^2`mc3zk@C@M6sRT2C1s(fFdrs6%$;Wx>6hhP{tMj+`>gO7~^@x%8~5"
    ")~x+Q)8BRX&8%91#r(R&vXp{bYs;GUM5kK0NZ0@7{^opsR@qup`tDC=j4Hp1EI+YMAlX;9$$iGlnUPmari8s~Jax"
    "5%n4jBt2@CtwG~!%iawC*?(l>SKJ%{VchyLP@2mUA0&ZNDaUf1(}#Pq($!-_J@s?TM5pEL=z9`|j_iS2n*RD}F(s"
    "gv7)Ba%0JrpuSt(K!K5yMtpg4)tkS+i%vEq{VgTJ^Vt9ZU;0~E8}i3m++rSNldTB_b4;x)VCeVrH2;O^|^WIuS<c"
    "yp9|kMbxHqUCoH??KM&r!F>5V9%d0+l#{1t-y&Fr8`b?Izu9!rw&mVF3kNbCew`?(XIJ)w(k;BXRCDu(l;;ha_%9"
    "9K~Kf9DooLJh@DoI<t%;`q{jeP=J;aSxucV+3Cv<cJt{O0oRMcU!I1c8Wpv(;yY<?+mGCa;IxT!|GW%wAVCD9oY8"
    "s%=_ryTSX(eJri=)lC)`ieiqW|4bG~YmBLnMO3WuX4fg{5e-D*G%?!#@}1<iX8>{0#dPHwo5>#?tbgoqEBs;bF*0"
    "y<=BX4f?DydC5}C62X0JCR$2v_*&Zo+cSD7_@GjPLm<&(qBX^)&&Ssxj2_|k`xjUvkn-sorS`ayHQZJ*Dcmqi@^)"
    "1~mad|!QL_9{5?-VKv0aWU7Cakif2pM+=5=&JyI6`-#I^i_bq3eZ;p`YJ$Q1?a1Q|9lmo&b`#nCd1`~d4;i5T1Ry"
    "*ChID;EZ!Yg@!oLz(<!p3p5||6*$o}@s?ycVBH~kyvM;aV&pUp;t+J+LeL`ox3M<`KQKELayz>XYfuUI!pABlC)L"
    "Gbet#O}IFw~hC5;G!W^<DLXxcjGX<!_i-k*O)!lhYPirBQZ1HhD_EJ-M={&}IEO=+d`oyQ)&6XBlKqs*YH1ovT^7"
    "+A`S0&irM+zj78`9<VK}{?*Kw<)M|A0)_{q?p&SZ7qW4;k4F7I>g4d<&b6hF)FJ`lGIT_`cX)K?=H3sF=A4czGe|"
    "jirnCRe_l?A?7lTcoWDi|+y79X;!|JkAdp)R}YdvTo8FVdi_M@cN4R-C-K?i0C*NsowzH59RnT5Q%Eq78#WrR_6s"
    "G-q^-Ur^q9B~d`b!)*umFM>6bo;|A$Z?uex8O?WBQs(I%eO|hIS<(y+U`(2<jj;+`>)I`zPRpl<%)xW3Orep*fM8"
    "&XschvaWc28?})x<unjkUxsm3zt^VD)(qpM%kx!C*l;dm<B|#^JCzrJq{P;(D`-!MP^GQ}sHS(U$MyZ9H{<L~q>9"
    "c8SIJ$q{p!CPK6B`HC)TTzuvYh)A4y{=~Z0qeqJzCzI4Ib`%rP$&5U!Ugm+?}!0t$b^^CVf%I)IKv_*Oy$YT%N@|"
    "?xcqNlr#@69oPAraPQ$$3qIJ*?mq_d$o#Q?#5l5wXv?%SYHzaKP?E7P!||aU_1zYmT~=^7cWb9y{Lv-qP37m(E5_"
    "%~o;NxeefH(!kg>4|N3ZfCJBYK%)vuaN+%7CJudUnhvN&o;u-{;t({>vRx0~jzZU}gFes<lG?a=dx=0ickYVXnCr"
    "!Ula-zg9`_*K{yK6z{E1tR^MEBu(6rA~osv!)!;WY*Tta*iEYZG=Q!9-8sXYOkibH<G@6cJ21!fpz}hykFP)w@G!"
    "}?z2g}N~eo^o+b2gdn&yBUSBup>jr(@psySBb%VZc(AN$6x<Owz{1@tm71lOm&Odqd^ELZBgNA$*Y<oC4^VXc8Uy"
    "KbKnjeksQ16+DrH(&kYEAy$_@Sv{V(^R04-7}56>&#>l74fZYWm!L1$;iU(%ODl>a>0}7SHdMuWRTPQCWJGIQojt"
    "lS^w2lcy{lJ?Ln87;m$%_|E=$b#&0XyX6<gjh7(>@Al6b!Qb(sMm#w_!zpCIhZjS7hlm~vj>7Y{HXC=Kd8W=YHyZ"
    "J3EF2u14q!Foh1GeU^_@o3?@#ZPUG?0NwX<SOoYSqrN|Q6&%Uq%>0wYm=|NNAKoDhrq{a*=IRoyDh?kN4P^}1u*#"
    "9eVxQ;VL<Tb*pfoef^@xRSh#>d}0$m&wanS%qVd&n(~hLs3YugO>q$o$}v3X@cQcvmHatXU@s*tNv_?E!?*$@2RC"
    "@kCD%sJPU=mhnY=i!MVkCSIlyr?RVVNQkyzu_FqKG0Xv80hCPR8`)B7}J>3|bS8rDQ<f`w*^7e?w{dk^p6%7x1(i"
    "i^>ERXh=?I`cREcsdd>!hsxg)UXI$3}ga)pAFf1J@pS(`;tqJ*i~2YtXKiCr|F*9A#nal0Ij6RkHqQtUns-kH-3="
    "vHobRKN{<g#`>eN{%HK)I2!jgN3u$fH3cR&Z#$~qRUmR4l3(w(XCG{`G^8yoO=VI;S>7Ey^4>a=gYU%G6>dMYyQF"
    "D;J^m`!QY<lzx!Z9Gec^Ms^{!cmbxV9SJ>kXtSlqtmh|SOH{zL5EdNlp*VQxQ$Y_vJi7Wa9Fr*-d5-v$01h`AeI^"
    "*MGs!O%wL1<inqvwD26X(~&uphk|n#((WUa76N^Ap@;P#i^%m)l7G?r&9La&VCEYUQFX1s86hl^YML~Q&D`ca$kP"
    "!n(w?H_%!6+zOYwe`|C;9oWon^8a(&-C~q`8vUgESkX@)<)u*)uJ1bmAwFj7`6m7A`nuacFG3*H!xD4p9JpHJ!y*"
    "SJ`DG~9x7guy{gT!jewS?K5Z8tlP-8-vyI&XqW5Br+Ln}aF)?^FH<QaUU<"
)


def load_sdpc() -> bytes:
    data = zlib.decompress(base64.b85decode(_DATA))
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("packaged synthetic SDPC fixture integrity check failed")
    return data

